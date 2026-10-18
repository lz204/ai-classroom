"""LangGraph 状态机：5 类节点编排 + 基于 round 单调计数的条件路由。

教学闭环（7 轮）：
- r1 开篇讲解 -> 学生提问；r2 答疑并发布随堂测验 -> 学生答题；
- r3 讲评答题 -> 学生追问；r4 答疑收束并组织分组研讨 -> 学生分组研讨；
- r5 研讨总结 -> 助教批改；r6 布置作业 -> 管理员学情总结；r7 学情总结 -> END。
路由唯一依据是单调递增的 round 计数（仅由 teacher_node 写入），phase 仅作前端展示；
告警（学生超时/环节超时/整堂课超时）优先交给 manager 收束，manager 处理完按 resume_target
回到「本应去」的节点（修复「告警吞轮次」：学生超时不再导致学生发言环节被跳过）。
"""
import asyncio
import time

from langgraph.graph import END, StateGraph

import config
from agents.manager import ManagerAgent
from agents.student import StudentAgent
from agents.ta import TaAgent
from agents.teacher import TeacherAgent
from config import STUDENT_PERSONALITIES
from graph.grouping import build_groups
from graph.speaker_queue import SpeakerQueue, arbiter_node
from graph.state import ClassroomState
from llm_client import LLMClient, get_llm
from memory.layered_memory import LayeredMemory

# 学生 LLM 并发上限（避免一次性打满 QPM）
_semaphore = asyncio.Semaphore(5)

# 学生单次 LLM 调用超时（秒）：超时记为学生走神告警 {"type": "timeout", "student": 名字}
_STUDENT_LLM_TIMEOUT = 30.0

_OPTION_LETTERS = ("A", "B", "C", "D")

# 每轮（teacher 执行轮次）对应的老师展示阶段与环节描述。
# phase 是给前端展示和 /api 状态查询用的字段，路由仍以单调递增的 round 为唯一依据；
# 同一轮内各节点上报自己的阶段（student: quiz/answer/discuss、ta: grading、manager: summary），
# 保证每条消息的阶段标签与实际环节一致。
_ROUND_STAGE = {
    1: ("teaching", "开篇讲解：讲授本节核心概念"),
    2: ("answer", "答疑并发布随堂测验：回应第一轮学生提问"),
    3: ("answer", "讲评随堂测验：结合学生作答讲解"),
    4: ("answer", "答疑收束，并按性格互补组织分组研讨"),
    5: ("discuss", "研讨总结：归纳各小组讨论结论"),
    6: ("grading", "布置课后作业并说明要求"),
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


# ---------- 路由 ----------
def _route_target_for_round(round_val: int) -> str:
    """纯函数：仅按 round 决定「下一节点名」，不含任何告警分支。

    供 manager 处理完告警后计算 resume_target，保证告警处理不吞掉原定环节。
    """
    try:
        round_val = int(round_val or 0)
    except (TypeError, ValueError):
        round_val = 0
    if round_val <= 4:
        # r1-r4：学生提问/答题/追问/分组研讨
        return "student"
    if round_val == 5:
        return "ta"          # 助教批改
    if round_val == 6:
        return "manager"     # 学情总结
    return END               # round >= 7 结束


def route_by_phase(state: ClassroomState) -> str:
    """根据课堂轮次（round）与告警路由下一节点（teacher 节点出边）。"""
    # 1. 秩序告警优先处理（manager 处理完会按 resume_target 回到原定节点）
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
        return _route_target_for_round(round_val)

    return "manager"                  # 异常阶段由管理员兜底


def route_after_manager(state: ClassroomState) -> str:
    """manager 出边：告警处理完回到 resume_target 指定的节点（不能回到 manager 自身）。"""
    target = state.get("resume_target") or "teacher"
    if target not in ("teacher", "student", "ta", END):
        # 防御：非法/自环目标一律回 teacher，避免死循环
        target = "teacher"
    return target


# ---------- 状态初始化 ----------
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
    now = time.monotonic()
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
            # 分组研讨结果（r4 由 teacher 组织分组后写入，供学生节点与学情档案使用）
            "groups": [],
        },
        "order_alerts": [],
        "round": 0,
        # ---- P1 教学闭环状态 ----
        "quiz": None,
        "quiz_answers": [],
        "stage_started_at": now,
        "class_started_at": now,
        "resume_target": None,
    }


# ---------- 随堂测验文本辅助 ----------
def _option_text(options: list, index: int) -> str:
    """选项下标 -> "B. 选项内容" 文本（越界时给出占位）"""
    if isinstance(index, int) and 0 <= index < len(options):
        letter = _OPTION_LETTERS[index] if index < len(_OPTION_LETTERS) else str(index)
        return f"{letter}. {options[index]}"
    return "（未知）"


def _correct_answer_text(quiz: dict) -> str:
    """随堂测验正确答案的可读文本"""
    options = (quiz or {}).get("options") or []
    return _option_text(options, (quiz or {}).get("answer_index"))


def _quiz_review_text(quiz: dict, answers: list, students: list) -> str:
    """把题目、正确答案与学生作答拼成一段文本（注入老师讲评/助教批改 Prompt）。"""
    name_by_id = {s.student_id: s.name for s in students}
    parts = [
        f"题目：{quiz.get('question', '')}",
        f"正确答案：{_correct_answer_text(quiz)}",
    ]
    if answers:
        answer_lines = []
        for ans in answers:
            sid = ans.get("student_id", "")
            answer_lines.append(
                f"{name_by_id.get(sid, sid)}选了{_option_text(quiz.get('options') or [], ans.get('choice_index'))}"
            )
        parts.append("学生作答：" + "；".join(answer_lines))
    else:
        parts.append("学生作答：（暂无作答记录）")
    return "；".join(parts)


def _build_grading_target(quiz: dict, answers: list, students: list) -> str:
    """助教批改对象文本：必须包含题目、正确答案和学生作答（契约要求）。"""
    if not quiz:
        return "请批改以下作业内容：本节课随堂练习（暂无结构化题目与作答记录）"
    name_by_id = {s.student_id: s.name for s in students}
    options = quiz.get("options") or []
    lines = [
        "请批改以下【随堂测验】并给出讲评建议：",
        f"题目ID：{quiz.get('quiz_id', '')}",
        f"题目：{quiz.get('question', '')}",
        "选项：" + "；".join(_option_text(options, i) for i in range(len(options))),
        f"正确答案：{_correct_answer_text(quiz)}",
        f"知识点：{quiz.get('knowledge_point', '')}",
        "学生作答：",
    ]
    if answers:
        for ans in answers:
            sid = ans.get("student_id", "")
            choice_index = ans.get("choice_index")
            is_correct = choice_index == quiz.get("answer_index")
            lines.append(
                f"- {name_by_id.get(sid, sid)}（{sid}）选择 {_option_text(options, choice_index)}"
                f"{'（正确）' if is_correct else '（错误）'}"
            )
    else:
        lines.append("- （暂无学生作答记录）")
    return "\n".join(lines)


def _format_groups_brief(groups: list) -> str:
    """分组安排文本（注入老师 r4 环节描述，便于老师组织研讨）"""
    return "；".join(
        f"{g.get('name', '')}（{g.get('angle', '')}）：{'、'.join(g.get('members') or [])}"
        for g in groups
    )


def build_workflow(llm: LLMClient | None = None, retriever=None) -> StateGraph:
    """构建并编译 LangGraph 状态图。"""
    llm = llm or get_llm()

    teacher_agent = TeacherAgent(llm, retriever)
    ta_agent = TaAgent(llm, retriever)
    manager_agent = ManagerAgent(llm)

    # ---------- 节点实现 ----------
    async def teacher_node(state: ClassroomState) -> dict:
        """主讲老师节点：round 单调递增 + 按 round 重写 phase（单一事实来源）。

        每轮先做课堂超时检查（环节超时/整堂课超时 -> order_alerts），
        r2 同时发布随堂测验，r4 组织性格互补分组研讨。
        """
        current_round = state.get("round", 0) or 0
        new_round = current_round + 1
        phase = _phase_for_round(new_round)
        stage_desc = _stage_desc_for_round(new_round)
        topic = state.get("topic", "")
        students = state["memory"]["students"]

        # 1) 课堂超时管控：环节超时 / 整堂课超时 -> 追加告警，交由 manager 收束
        now = time.monotonic()
        class_started_at = state.get("class_started_at") or now
        stage_started_at = state.get("stage_started_at") or class_started_at
        elapsed_stage = now - stage_started_at
        elapsed_class = now - class_started_at
        alerts: list[dict] = []
        stage_overtime = elapsed_stage > config.STAGE_TIMEOUT_SECONDS
        if stage_overtime:
            alerts.append(
                {
                    "type": "overtime",
                    "stage": phase,
                    "elapsed": round(elapsed_stage, 2),
                    "limit": config.STAGE_TIMEOUT_SECONDS,
                    "round": new_round,
                }
            )
        if elapsed_class > config.CLASS_TIMEOUT_SECONDS:
            alerts.append(
                {
                    "type": "class_timeout",
                    "elapsed": round(elapsed_class, 2),
                    "limit": config.CLASS_TIMEOUT_SECONDS,
                }
            )

        # 2) r3 讲评答题：把题目/正确答案/学生作答注入环节描述
        quiz = state.get("quiz")
        if new_round == 3 and quiz:
            stage_desc = (
                f"{stage_desc}（{_quiz_review_text(quiz, state.get('quiz_answers') or [], students)}）"
            )

        # 3) r4 组织分组研讨：性格互补分组写入 memory["groups"]，并告知老师分组安排
        memory_update = None
        if new_round == 4:
            groups = build_groups(students, config.STUDENT_GROUP_COUNT)
            memory_update = {**state["memory"], "groups": groups}
            if groups:
                stage_desc = f"{stage_desc}（分组安排：{_format_groups_brief(groups)}）"

        # 4) 加载记忆上下文并生成老师消息（r2 出题环节要求同时输出 quiz）
        memory = state["memory"]["layered_memory"]
        memory_vars = memory.load_memory_variables({"input": stage_desc})
        msg = await teacher_agent.respond(
            topic=topic,
            stage_desc=stage_desc,
            memory_text="\n".join(
                v for v in (memory_vars["history"], memory_vars["summary"]) if v
            ),
            with_quiz=(new_round == 2),
        )

        # 本轮对话写入记忆，并触发异步摘要压缩
        memory.save_context(stage_desc, msg.get("content", ""))
        await memory.compress_if_needed()

        update = {
            "round": new_round,
            "phase": phase,
            "current_speaker": "teacher",
            "messages": [msg],
        }
        if alerts:
            # 有超时告警：经 reducer 追加（不能返回 []，否则会被当成清空）
            update["order_alerts"] = alerts
        # 正常时刷新环节起始时间；环节已超时则保留旧值（下一轮继续提示/收束）
        if not stage_overtime:
            update["stage_started_at"] = time.monotonic()
        if msg.get("quiz"):
            update["quiz"] = msg["quiz"]
        if memory_update is not None:
            update["memory"] = memory_update
        return update

    async def student_group_node(state: ClassroomState) -> dict:
        """学生群节点：节点内部并行调用 LLM，失败不阻塞整体。

        - r2：已有随堂测验则逐个答题（answer_quiz），否则回退普通提问；
        - r4：按性格互补分组，逐学生携带 group 参与研讨；
        - 其余轮次：普通提问/追问。
        """
        students = state["memory"]["students"]
        messages = state.get("messages") or []
        question = messages[-1]["content"] if messages else ""
        current_round = state.get("round", 0) or 0
        quiz = state.get("quiz")
        groups = state["memory"].get("groups") or []
        if current_round == 4 and not groups:
            # 兜底：老师节点未写入分组时，学生节点自行分组（保证 r4 一定有分组）
            groups = build_groups(students, config.STUDENT_GROUP_COUNT)
        group_by_student: dict[str, dict] = {}
        for group in groups:
            for sid in group.get("member_ids") or []:
                group_by_student[sid] = group

        if current_round == 2:
            phase = "answer"          # r2：随堂测验作答环节
        elif current_round == 4:
            phase = "discuss"         # r4：分组研讨环节
        else:
            phase = "quiz"            # r1/r3：提问/追问环节
        use_quiz = bool(current_round == 2 and quiz)

        # 学生发言前先按性格进入发言队列（学霸型优先级更高），
        # 队列每轮新建（避免跨轮累积旧学生），经 memory["queue"] 交给仲裁器排序
        queue = SpeakerQueue()
        for s in students:
            queue.enqueue(s.student_id, s.personality)

        async def _respond_with_limit(student):
            group = group_by_student.get(student.student_id) if current_round == 4 else None
            async with _semaphore:
                # 超时 30s 触发 asyncio.TimeoutError，由 gather 捕获后记入走神告警
                if use_quiz:
                    return await asyncio.wait_for(
                        student.answer_quiz(quiz), timeout=_STUDENT_LLM_TIMEOUT
                    )
                if group is not None:
                    return await asyncio.wait_for(
                        student.respond(question, group=group), timeout=_STUDENT_LLM_TIMEOUT
                    )
                return await asyncio.wait_for(
                    student.respond(question), timeout=_STUDENT_LLM_TIMEOUT
                )

        raw_responses = await asyncio.gather(
            *[_respond_with_limit(s) for s in students], return_exceptions=True
        )

        valid, alerts, answers = [], [], []
        for idx, resp in enumerate(raw_responses):
            student = students[idx]
            if isinstance(resp, Exception):
                alerts.append({"type": "timeout", "student": student.name})
                continue
            msg = dict(resp)
            if current_round == 4:
                # r4 每条学生消息都必须带 group（分组覆盖全部学生）
                group = group_by_student.get(student.student_id)
                if group is not None:
                    msg["group"] = group
            elif use_quiz and msg.get("answer"):
                # r2 作答经 reducer 累积进 quiz_answers（每条含 student_id/choice_index/choice_text）
                answers.append(msg["answer"])
            valid.append(msg)

        update = {
            "new_messages": valid,
            "phase": phase,
            "memory": {**state["memory"], "groups": groups, "queue": queue},
        }
        if alerts:
            # 告警经 reducer 追加；无告警时不返回该字段，避免误触发「清空」语义
            update["order_alerts"] = alerts
        if answers:
            update["quiz_answers"] = answers
        return update

    async def ta_node(state: ClassroomState) -> dict:
        """助教节点：批改随堂测验（target 文本包含题目、正确答案与学生作答）。"""
        quiz = state.get("quiz") or {}
        answers = state.get("quiz_answers") or []
        students = state["memory"]["students"]
        target = _build_grading_target(quiz, answers, students)
        msg = await ta_agent.respond(target)
        return {
            "messages": [msg],
            "current_speaker": "ta",
            "phase": "grading",
        }

    async def manager_node(state: ClassroomState) -> dict:
        """管理员节点：处理秩序/超时告警（处理后清空），或输出学情总结。

        告警处理完必须回到「本轮原定应去的节点」（resume_target），
        否则会吞掉学生发言/批改环节；无告警时照旧回 teacher。
        """
        alerts = state.get("order_alerts", []) or []
        had_alerts = bool(alerts)
        task = (
            "处理以下课堂告警并给出收束动作：" + str(alerts)
            if had_alerts
            else "课堂已结束，输出学情总结报告"
        )
        state_summary = (
            f"课堂主题：{state.get('topic', '')}；"
            f"当前轮次：{state.get('round', 0)}；"
            f"消息条数：{len(state.get('messages', []))}"
        )
        msg = await manager_agent.respond(task, state_summary)

        current_round = state.get("round", 0) or 0
        if had_alerts:
            resume_target = _route_target_for_round(current_round)
            if resume_target == "manager":
                # manager 不能回到自身（死循环），回退到 teacher
                resume_target = "teacher"
        else:
            resume_target = "teacher"
        return {
            "messages": [msg],
            "current_speaker": "manager",
            "phase": "summary",
            # 清空告警，避免路由因 order_alerts 非空反复跳转 manager 死循环
            "order_alerts": [],
            # 告警处理后的去向：本应去的学生/助教/结束节点，修复「告警吞轮次」
            "resume_target": resume_target,
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
    # manager 出边改为条件边：告警处理完回到 resume_target（不能回 manager 自身）
    graph.add_conditional_edges(
        "manager",
        route_after_manager,
        {
            "teacher": "teacher",
            "student": "student",
            "ta": "ta",
            END: END,
        },
    )
    graph.set_entry_point("teacher")
    return graph.compile()
