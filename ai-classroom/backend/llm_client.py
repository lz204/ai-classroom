"""LLM 客户端：真实 GLM（OpenAI 兼容接口）与内置 Mock 双模式。

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


class OpenAIClient(LLMClient):
    """真实 GLM 客户端（OpenAI 兼容协议）"""

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


# ============ 内置 Mock LLM ============
# 以下正则用于从各 Agent 的 Prompt 中提取角色与身份标记
_RE_TEACHER = re.compile(r"【主讲老师】")
_RE_TA = re.compile(r"【助教】")
_RE_STUDENT = re.compile(r"【学生】")
_RE_MANAGER = re.compile(r"【课堂管理员】")
_RE_STUDENT_ID = re.compile(r"【学生ID】\s*([A-Za-z0-9_\-]+)")
_RE_STUDENT_NAME = re.compile(r"【学生姓名】\s*([^\n]+)")
_RE_TOPIC = re.compile(r"【主题】\s*([^\n]+)")


class MockLLM(LLMClient):
    """内置 Mock LLM：根据 Prompt 中的角色标记返回结构化的模拟 JSON，用于无 Key 演示"""

    def __init__(self):
        # 模拟"课堂主题"占位（实际主题由 teacher 节点写入 state）
        self._topic = "高等数学 第一章 极限与连续"

    async def ainvoke(self, prompt: str) -> str:
        # 模拟真实 LLM 的少量网络延迟，让前端时序更接近真实
        await asyncio.sleep(0.3)
        return self._build_response(prompt)

    def _build_response(self, prompt: str) -> str:
        """根据角色标记构造对应 JSON 输出"""
        topic_match = _RE_TOPIC.search(prompt)
        if topic_match:
            self._topic = topic_match.group(1).strip()

        if _RE_TEACHER.search(prompt):
            return self._teacher_response()
        if _RE_STUDENT.search(prompt):
            return self._student_response(prompt)
        if _RE_TA.search(prompt):
            return self._ta_response()
        if _RE_MANAGER.search(prompt):
            return self._manager_response()
        # 未识别角色：输出通用兜底结构（不影响上层校验）
        return json.dumps({"speaker": "unknown", "content": "Mock LLM 未识别角色标记"}, ensure_ascii=False)

    def _teacher_response(self) -> str:
        """主讲老师：授课/答疑内容，带引用来源"""
        return json.dumps(
            {
                "speaker": "teacher",
                "content": f"同学们好，今天我们学习《{self._topic}》。"
                           "首先明确本节核心概念：函数在某点的极限描述了当自变量趋近该点时函数值的稳定趋势。"
                           "这是后续导数与积分的基础，请大家结合教材第 2 章例题理解。",
                "citation": ["教材第2章 极限与连续"],
            },
            ensure_ascii=False,
        )

    def _student_response(self, prompt: str) -> str:
        """学生：提问/回答，speaker 字段为学生 id（与发言队列一致）"""
        sid = _RE_STUDENT_ID.search(prompt)
        name = _RE_STUDENT_NAME.search(prompt)
        speaker = sid.group(1) if sid else "student_unknown"
        student_name = name.group(1).strip() if name else speaker
        return json.dumps(
            {
                "speaker": speaker,
                "content": f"我是{student_name}。想请教老师：如果函数在某点无定义，但左右极限存在且相等，"
                           "是否可以认为该点极限存在？希望老师结合例子说明。",
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
