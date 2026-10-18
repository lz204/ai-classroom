"""LLM 客户端：真实 LLM（OpenAI 兼容接口，默认阿里百炼 DashScope）与内置 Mock 双模式。

统一对外暴露 `ainvoke(prompt) -> str`，上层 Agent 无需感知底层实现。
配置了 LLM_API_KEY 时走真实调用；未配置时自动降级为 Mock，保证无 Key 也能跑通课堂全流程。
"""
import asyncio
import json
import re

from openai import APIConnectionError, AsyncOpenAI, RateLimitError
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from config import (
    LLM_API_KEY,
    LLM_BASE_URL,
    LLM_MODEL,
    LLM_TIMEOUT,
)


class LLMClient:
    """LLM 客户端统一接口"""

    async def ainvoke(self, prompt: str) -> str:
        """调用 LLM，返回文本内容（真实模式取 message.content，Mock 模式返回模拟 JSON）"""
        raise NotImplementedError

    async def chat_with_tools(
        self, messages: list[dict], tools: list[dict] | None = None
    ) -> dict:
        """带工具（Function Calling）的一轮对话，供 Tool-Use Agent 使用。

        :return: {"content": str | None, "tool_calls": [{"id", "name", "arguments"}]}
                 arguments 为 JSON 字符串（与 OpenAI 协议一致）
        """
        raise NotImplementedError


class OpenAIClient(LLMClient):
    """真实 LLM 客户端（OpenAI 兼容协议）"""

    def __init__(self, api_key: str, base_url: str, model: str, timeout: float):
        self._model = model
        self._timeout = timeout
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url)

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=2, max=8),
        retry=retry_if_exception_type((RateLimitError, APIConnectionError, TimeoutError)),
    )
    async def ainvoke(self, prompt: str) -> str:
        """带指数退避重试的调用：限流/连接错误/超时按 2s/4s/8s 重试 3 次"""
        resp = await asyncio.wait_for(
            self._client.chat.completions.create(
                model=self._model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.7,
            ),
            timeout=self._timeout,
        )
        return resp.choices[0].message.content or ""

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=2, max=8),
        retry=retry_if_exception_type((RateLimitError, APIConnectionError, TimeoutError)),
    )
    async def chat_with_tools(
        self, messages: list[dict], tools: list[dict] | None = None
    ) -> dict:
        """Function Calling：把 tool_calls 归一化为 {id, name, arguments(JSON 字符串)}。"""
        kwargs: dict = {
            "model": self._model,
            "messages": messages,
            "temperature": 0.2,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        resp = await asyncio.wait_for(
            self._client.chat.completions.create(**kwargs),
            timeout=self._timeout,
        )
        message = resp.choices[0].message
        tool_calls = []
        for idx, call in enumerate(getattr(message, "tool_calls", None) or []):
            function = getattr(call, "function", None)
            if function is None:
                continue
            tool_calls.append(
                {
                    "id": getattr(call, "id", None) or f"call_{idx}",
                    "name": function.name or "",
                    "arguments": function.arguments or "{}",
                }
            )
        return {"content": message.content, "tool_calls": tool_calls}


# ============ 内置 Mock LLM ============
# 以下正则用于从各 Agent 的 Prompt 中提取角色与身份标记
_RE_TEACHER = re.compile(r"【主讲老师】")
_RE_TA = re.compile(r"【助教】")
_RE_STUDENT = re.compile(r"【学生】")
_RE_MANAGER = re.compile(r"【课堂管理员】")
_RE_STUDENT_ID = re.compile(r"【学生ID】\s*([A-Za-z0-9_\-]+)")
_RE_STUDENT_NAME = re.compile(r"【学生姓名】\s*([^\n]+)")
_RE_TOPIC = re.compile(r"【主题】\s*([^\n]+)")
# P1 教学闭环新增标记：随堂测验（老师）与答题模式（学生）
_RE_QUIZ = re.compile(r"【随堂测验】")
_RE_ANSWER_MODE = re.compile(r"【答题模式】")
_RE_QUIZ_ID = re.compile(r"【题目ID】\s*([^\n]+)")
# 选项行：形如 "A. 选项内容" / "B、选项内容"（学生答题 Prompt 中由 workflow/agent 拼出）
_RE_OPTION_LINE = re.compile(r"^[ \t]*([A-D])[.、)]\s*(.+?)[ \t]*$", re.MULTILINE)
# 选项下标 -> 字母
_OPTION_LETTERS = ("A", "B", "C", "D")

# ---- Tool-Use（Mock Function Calling）触发词 / 算式提取 ----
_RE_TOOL_CALC = re.compile(r"计算|算一下|算一算|求值|等于多少|结果是多少")
# 连续算式片段（数字/四则/幂/括号），用于从自然语言里提取表达式
_RE_TOOL_CALC_RUN = re.compile(r"[0-9+\-*/%(). ]+")
_RE_TOOL_KB = re.compile(r"极限|连续|导数|积分|定理|知识库|教材|概念")
_RE_TOOL_TIME = re.compile(r"几点|现在时间|当前时间|今天几号|日期")
# 中文/全角符号 -> ASCII 算式符号
_RE_TOOL_TRANSLATION = str.maketrans(
    {"×": "*", "÷": "/", "（": "(", "）": ")", "－": "-", "＋": "+", "^": "**"}
)


def _extract_expression(text: str) -> str | None:
    """从自然语言中提取最长的算式片段（不足则返回 None）。"""
    normalized = text.translate(_RE_TOOL_TRANSLATION)
    candidates = [
        run.strip()
        for run in _RE_TOOL_CALC_RUN.findall(normalized)
        if any(ch.isdigit() for ch in run)
    ]
    if not candidates:
        return None
    expression = max(candidates, key=len).strip(" +-*/%")
    return expression or None


class MockLLM(LLMClient):
    """内置 Mock LLM：根据 Prompt 中的角色标记返回结构化的模拟 JSON，用于无 Key 演示"""

    def __init__(self):
        # 模拟"课堂主题"占位（实际主题由 teacher 节点写入 state）
        self._topic = "高等数学 第一章 极限与连续"

    async def ainvoke(self, prompt: str) -> str:
        # 模拟真实 LLM 的少量网络延迟，让前端时序更接近真实
        await asyncio.sleep(0.3)
        return self._build_response(prompt)

    # ---------- Tool-Use（Function Calling）Mock ----------
    async def chat_with_tools(
        self, messages: list[dict], tools: list[dict] | None = None
    ) -> dict:
        """确定性的 Mock Function Calling，保证 ToolAgent 单测不依赖网络/真实 API。

        规则：
        - messages 里已有 role=tool 的结果 -> 返回最终回答（不再调用工具）；
        - 用户问题含「计算/算一下...」-> 返回 calculate tool_call（自动提取算式）；
        - 含「极限/连续/知识库/教材...」-> 返回 search_knowledge_base tool_call；
        - 含「几点/当前时间...」-> 返回 get_current_time tool_call；
        - 其它 -> 直接返回最终回答。
        """
        await asyncio.sleep(0)  # 保持异步语义但不加延迟，测试快速
        available = set()
        for tool in tools or []:
            function = (tool or {}).get("function") or {}
            name = function.get("name")
            if name:
                available.add(name)
        if not available:
            available = {"calculate", "search_knowledge_base", "text_stats"}

        # 已经有工具执行结果：生成最终回答（模拟 ReAct 的 Final Answer 阶段）
        tool_messages = [m for m in messages if (m or {}).get("role") == "tool"]
        if tool_messages:
            observation = tool_messages[-1].get("content") or ""
            return {"content": f"根据工具执行结果：{observation}", "tool_calls": []}

        question = ""
        for message in reversed(messages or []):
            if (message or {}).get("role") == "user":
                question = str(message.get("content") or "")
                break

        if _RE_TOOL_CALC.search(question):
            expression = _extract_expression(question)
            if expression and "calculate" in available:
                return self._mock_tool_call(
                    "calculate", {"expression": expression}, "call_mock_1"
                )
            # 触发词但提取不到算式：直接回答，避免无意义的工具循环
            return {
                "content": f"（Mock 回答）请提供具体算式，例如 12*3+5。原问题：{question[:80]}",
                "tool_calls": [],
            }
        if _RE_TOOL_KB.search(question) and "search_knowledge_base" in available:
            return self._mock_tool_call(
                "search_knowledge_base",
                {"query": question[:120], "top_k": 3},
                "call_mock_2",
            )
        if _RE_TOOL_TIME.search(question) and "get_current_time" in available:
            return self._mock_tool_call("get_current_time", {}, "call_mock_3")
        return {
            "content": f"（Mock 直接回答）关于「{question[:80]}」：这是一个示例回答。",
            "tool_calls": [],
        }

    @staticmethod
    def _mock_tool_call(name: str, arguments: dict, call_id: str) -> dict:
        """构造一次 tool_call（OpenAI 协议风格：arguments 为 JSON 字符串）"""
        return {
            "content": None,
            "tool_calls": [
                {
                    "id": call_id,
                    "name": name,
                    "arguments": json.dumps(arguments, ensure_ascii=False),
                }
            ],
        }

    def _build_response(self, prompt: str) -> str:
        """根据角色标记构造对应 JSON 输出"""
        topic_match = _RE_TOPIC.search(prompt)
        if topic_match:
            self._topic = topic_match.group(1).strip()

        if _RE_TEACHER.search(prompt):
            # 老师 Prompt 中只有「随堂测验」环节才会带【随堂测验】约束，此时需一并输出 quiz
            return self._teacher_response(with_quiz=bool(_RE_QUIZ.search(prompt)))
        if _RE_STUDENT.search(prompt):
            return self._student_response(prompt)
        if _RE_TA.search(prompt):
            return self._ta_response()
        if _RE_MANAGER.search(prompt):
            return self._manager_response()
        # 未识别角色：输出通用兜底结构（不影响上层校验）
        return json.dumps({"speaker": "unknown", "content": "Mock LLM 未识别角色标记"}, ensure_ascii=False)

    def _teacher_response(self, with_quiz: bool = False) -> str:
        """主讲老师：授课/答疑内容，带引用来源；r2 随堂测验环节额外返回 quiz 结构。"""
        payload = {
            "speaker": "teacher",
            "content": f"同学们好，今天我们学习《{self._topic}》。"
                       "首先明确本节核心概念：函数在某点的极限描述了当自变量趋近该点时函数值的稳定趋势。"
                       "这是后续导数与积分的基础，请大家结合教材第 2 章例题理解。",
            "citation": ["教材第2章 极限与连续"],
        }
        if with_quiz:
            payload["content"] += "下面做一道随堂测验，检验大家对极限定义的理解。"
            payload["quiz"] = {
                "quiz_id": "quiz_1",
                "question": f"关于《{self._topic}》中函数极限的描述，下列说法正确的是？",
                "options": [
                    "函数在某点的极限与函数在该点是否有定义无关",
                    "函数在某点的极限一定等于函数在该点的函数值",
                    "函数在某点的极限不存在时，左右极限也一定都不存在",
                    "函数在某点的极限存在，则该函数在该点必定连续",
                ],
                "answer_index": 1,
                "knowledge_point": "极限的定义",
            }
        return json.dumps(payload, ensure_ascii=False)

    def _student_response(self, prompt: str) -> str:
        """学生：普通提问/研讨，或【答题模式】下返回随堂测验作答（speaker 为学生 id）。"""
        sid = _RE_STUDENT_ID.search(prompt)
        name = _RE_STUDENT_NAME.search(prompt)
        speaker = sid.group(1) if sid else "student_unknown"
        student_name = name.group(1).strip() if name else speaker
        if _RE_ANSWER_MODE.search(prompt):
            return self._student_answer_response(prompt, speaker, student_name)
        return json.dumps(
            {
                "speaker": speaker,
                "content": f"我是{student_name}。想请教老师：如果函数在某点无定义，但左右极限存在且相等，"
                           "是否可以认为该点极限存在？希望老师结合例子说明。",
            },
            ensure_ascii=False,
        )

    def _student_answer_response(self, prompt: str, speaker: str, student_name: str) -> str:
        """学生答题模式：固定选择 B（choice_index=1），choice_text 取对应选项原文。"""
        quiz_id = _RE_QUIZ_ID.search(prompt)
        options = [text.strip() for _, text in _RE_OPTION_LINE.findall(prompt)]
        # 答案解析优先级：第2个选项（B）；选项不足时退化为第一个选项
        choice_index = 1 if len(options) > 1 else 0
        choice_text = options[choice_index] if choice_index < len(options) else ""
        answer = {
            "student_id": speaker,
            "quiz_id": quiz_id.group(1).strip() if quiz_id else "",
            "choice_index": choice_index,
            "choice_text": choice_text,
        }
        return json.dumps(
            {
                "speaker": speaker,
                "content": f"我是{student_name}，我选择 {_OPTION_LETTERS[choice_index]}：{choice_text}",
                "answer": answer,
            },
            ensure_ascii=False,
        )

    def _ta_response(self) -> str:
        """助教：作业批改结果"""
        return json.dumps(
            {
                "speaker": "ta",
                "content": "已批改本次作业。大部分同学对极限定义理解到位，"
                           "个别同学在等价无穷小替换时忽略了使用条件，建议复习第 2 章例 4。",
                "citation": ["评分规则 Prompt"],
            },
            ensure_ascii=False,
        )

    def _manager_response(self) -> str:
        """课堂管理员：秩序干预或学情总结"""
        return json.dumps(
            {
                "speaker": "manager",
                "content": "课堂秩序正常，未发现超时环节；"
                           "总结：本节知识点掌握度整体良好，薄弱点集中在'无穷小量阶的比较'，建议课后巩固。",
            },
            ensure_ascii=False,
        )


def get_llm() -> LLMClient:
    """工厂方法：配置了 API Key 返回真实客户端，否则返回 Mock"""
    if LLM_API_KEY:
        return OpenAIClient(
            api_key=LLM_API_KEY,
            base_url=LLM_BASE_URL,
            model=LLM_MODEL,
            timeout=LLM_TIMEOUT,
        )
    return MockLLM()
