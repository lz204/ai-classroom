"""LangGraph 状态机：5 类节点编排 + 基于 round 单调计数的条件路由。

与项目文档 6.1.2/6.1.3/6.1.4 对应：
- 状态机闭环：teacher -> student -> arbiter -> teacher -> ta -> teacher -> manager -> teacher -> END；
- 路由唯一依据是单调递增的 round 计数（仅由 teacher_node 写入），phase 仅作前端展示；
- 学生群节点内部并发调用 LLM，仲裁器对新增消息排序后串行写入。
"""
import asyncio

from langgraph.graph import END, StateGraph

from agents.manager import ManagerAgent
from agents.student import StudentAgent
from agents.ta import TaAgent
from agents.teacher import TeacherAgent
from config import STUDENT_PERSONALITIES
from graph.speaker_queue import SpeakerQueue, arbiter_node
from graph.state import ClassroomState
from llm_client import LLMClient, get_llm
from memory.layered_memory import LayeredMemory

# 学生 LLM 并发上限（避免一次性打满 QPM）
_semaphore = asyncio.Semaphore(5)

# 每轮（teacher 执行轮次）对应的老师展示阶段与环节描述。
# phase 是给前端展示和 /api 状态查询用的字段，路由仍以单调递增的 round 为唯一依据；
# 同一轮内各节点上报自己的阶段（student: quiz/discuss、ta: grading、manager: summary），
# 保证每条消息的阶段标签与实际环节一致，而不是整体错位一轮。
_ROUND_STAGE = {
    1: ("teaching", "开篇讲解：讲授本节核心概念"),
    2: ("answer", "答疑：回应第一轮学生提问"),
    3: ("answer", "答疑：回应第二轮学生提问"),
    4: ("answer", "答疑：回应第三轮学生提问，并组织小组研讨"),
    5: ("discuss", "研讨总结：归纳小组研讨结论"),
    6: ("grading", "布置并批改课后作业"),
    7: ("summary", "输出学情总结并宣布课堂结束"),
}
_PHASE_BY_ROUND = {r: phase for r, (phase, _) in _ROUND_STAGE.items()}
_STAGE_DESC_BY_ROUND = {r: desc for r, (_, desc) in _ROUND_STAGE.items()}


def _phase_for_round(new_round: int) -> str:
    """round 递增后对应的 phase 展示值（前端据此切换界面态）"""
    return _PHASE_BY_ROUND.get(new_round, "teaching")


def _stage_desc_for_round(new_round: int) -> str:
    """round 递增后对应的环节描述（注入老师 Prompt）"""
    return _STAGE_DESC_BY_ROUND.get(new_round, "课堂收尾")


def route_by_phase(state: ClassroomState) -> str:
    """根据课堂轮次（round）与告警路由下一节点（与项目文档 6.1.3 一致）。"""
    # 1. 秩序告警优先处理
    if state.get("order_alerts"):
        return "manager"

    phase = state.get("phase")
    round_val = state.get("round", 0) or 0

    if phase == "end":
        return END

    # 2. 以单调递增的 round 为唯一依据决定下一节点（不信任外部可能被篡改的 phase）
    if phase in ("teaching", "quiz", "answer", "discuss", "grading", "summary"):
        if round_val == 0:
            return "teacher"          # 开篇讲解
        elif round_val <= 3:
            return "student"          # 3 轮学生提问/答疑
        elif round_val == 4:
            return "student"          # 小组研讨
        elif round_val == 5:
            return "ta"               # 助教批改
        elif round_val == 6:
            return "manager"          # 学情总结
        else:
            return END                # round >= 7 结束

    return "manager"                  # 异常阶段由管理员兜底


def create_initial_state(topic: str, llm: LLMClient) -> dict:
    """初始化课堂共享状态：创建学生智能体群、发言队列与三层记忆。"""
    students = [
        StudentAgent(
            llm=llm,
            student_id=f"student_{i + 1}",
            name=f"同学{i + 1}",
            personality=personality,
        )
        for i, personality in enumerate(STUDENT_PERSONALITIES)
    ]
    return {
        "messages": [],
        "new_messages": [],
        "current_speaker": "teacher",
        "phase": "teaching",
        "topic": topic,
        "memory": {
            "students": students,
            "queue": SpeakerQueue(),
            "layered_memory": LayeredMemory(llm, window_size=10, summary_threshold=20),
        },
        "order_alerts": [],
        "round": 0,
    }


def build_workflow(llm: LLMClient | None = None, retriever=None) -> StateGraph:
    """构建并编译 LangGraph 状态图。"""
    llm = llm or get_llm()

    teacher_agent = TeacherAgent(llm, retriever)
    ta_agent = TaAgent(llm, retriever)
    manager_agent = ManagerAgent(llm)

    # ---------- 节点实现 ----------
    async def teacher_node(state: ClassroomState) -> dict:
        """主讲老师节点：round 单调递增 + 按 round 重写 phase（单一事实来源）。

        即使外部（前端按钮/LLM 输出）篡改了 phase，这里也会按 round 重新纠正，
        保证课堂顺序固定，避免「授课 <-> 提问」来回跳导致的死循环。
        """
        current_round = state.get("round", 0) or 0
        new_round = current_round + 1
        phase = _phase_for_round(new_round)
        stage_desc = _stage_desc_for_round(new_round)
        topic = state.get("topic", "")

        # 加载记忆上下文注入老师 Prompt
        memory = state["memory"]["layered_memory"]
        memory_vars = memory.load_memory_variables({"input": stage_desc})

        # 生成授课/答疑内容
        msg = await teacher_agent.respond(
            topic=topic,
            stage_desc=stage_desc,
            memory_text="\n".join(
                v for v in (memory_vars["history"], memory_vars["summary"]) if v
            ),
        )

        # 本轮对话写入记忆，并触发异步摘要压缩
        memory.save_context(stage_desc, msg.get("content", ""))
        await memory.compress_if_needed()

        return {
            "round": new_round,
            "phase": phase,
            "current_speaker": "teacher",
            "messages": [msg],
        }

    async def student_group_node(state: ClassroomState) -> dict:
        """学生群节点：节点内部并行调用 LLM，失败不阻塞整体（与项目文档 6.1.4 一致）。"""
        students = state["memory"]["students"]
        question = state["messages"][-1]["content"] if state["messages"] else ""
        # 学生阶段：round 4 为小组研讨，其余轮次为随堂提问/追问
        current_round = state.get("round", 0) or 0
        phase = "discuss" if current_round == 4 else "quiz"

        # 学生发言前先按性格进入发言队列（学霸型优先级更高），
        # 队列每轮新建（避免跨轮累积旧学生），经 memory["queue"] 交给仲裁器排序
        queue = SpeakerQueue()
        for s in students:
            queue.enqueue(s.student_id, s.personality)

        async def _respond_with_limit(student):
            async with _semaphore:
                # 超时 30s 触发 asyncio.TimeoutError，由 gather 捕获后记入走神告警
                return await asyncio.wait_for(student.respond(question), timeout=30.0)

        raw_responses = await asyncio.gather(
            *[_respond_with_limit(s) for s in students], return_exceptions=True
        )

        valid, alerts = [], []
        for idx, resp in enumerate(raw_responses):
            if isinstance(resp, Exception):
                alerts.append({"type": "timeout", "student": students[idx].name})
            else:
                valid.append(resp)
        # 只产出"本次新增消息"，由仲裁器排序后统一写入 messages；
        # 告警也通过 return 触发 reducer 合并，而不是直接 mutate state；
        # memory 通过 return 写回（LangGraph 节点不应直接修改传入的 state 快照）
        return {
            "new_messages": valid,
            "order_alerts": alerts,
            "phase": phase,
            "memory": {**state["memory"], "queue": queue},
        }

    async def ta_node(state: ClassroomState) -> dict:
        """助教节点：基于最近消息批改作业。"""
        latest = state["messages"][-1]["content"] if state["messages"] else "本节课作业"
        msg = await ta_agent.respond(f"请批改以下作业内容：{latest}")
        return {
            "messages": [msg],
            "current_speaker": "ta",
            "phase": "grading",
        }

    async def manager_node(state: ClassroomState) -> dict:
        """管理员节点：处理秩序告警（处理后必须清空）或输出学情总结。"""
        alerts = state.get("order_alerts", [])
        task = (
            "处理以下秩序告警并给出收束动作：" + str(alerts)
            if alerts
            else "课堂已结束，输出学情总结报告"
        )
        state_summary = (
            f"课堂主题：{state.get('topic', '')}；"
            f"当前轮次：{state.get('round', 0)}；"
            f"消息条数：{len(state.get('messages', []))}"
        )
        msg = await manager_agent.respond(task, state_summary)
        return {
            "messages": [msg],
            "current_speaker": "manager",
            "phase": "summary",
            # 清空告警，避免路由因 order_alerts 非空反复跳转 manager 死循环
            "order_alerts": [],
        }

    # ---------- 状态图构建 ----------
    graph = StateGraph(ClassroomState)
    graph.add_node("teacher", teacher_node)
    graph.add_node("student", student_group_node)
    graph.add_node("ta", ta_node)
    graph.add_node("manager", manager_node)
    graph.add_node("arbiter", arbiter_node)

    # 条件路由：映射表的 key 必须是 route_by_phase 返回的「节点名」，不是 phase 名
    graph.add_conditional_edges(
        "teacher",
        route_by_phase,
        {
            "teacher": "teacher",
            "student": "student",
            "ta": "ta",
            "manager": "manager",
            END: END,
        },
    )
    # 学生并发生成完后，由仲裁器归并排序；仲裁器不直接接 student，否则会形成死循环
    graph.add_edge("student", "arbiter")
    graph.add_edge("arbiter", "teacher")
    graph.add_edge("ta", "teacher")
    graph.add_edge("manager", "teacher")
    graph.set_entry_point("teacher")
    return graph.compile()
