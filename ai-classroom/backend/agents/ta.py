"""助教 Agent：答疑解惑、作业批改、补充资料检索。"""
import asyncio

from pydantic import BaseModel, Field, field_validator

from agents.base import BaseAgent


class TaMessage(BaseModel):
    """助教消息结构"""

    speaker: str
    content: str
    citation: list[str] = Field(default_factory=list)

    @field_validator("citation", mode="before")
    @classmethod
    def _citation_none_to_empty(cls, v):
        """容忍 LLM 输出 citation=null，统一转为空列表"""
        if v is None:
            return []
        return v


_SYSTEM_PROMPT = """你是一位耐心细致的【助教】。
【批改/答疑对象】
{target}
【参考内容】
{context}
约束：
1. 必须基于【参考内容】给出批改意见或答疑，标注引用来源；
2. 不可发起授课，不可中断课堂；只对当前题目/问题作答。
输出格式必须严格遵循以下 JSON：
{{
  "speaker": "ta",
  "content": "批改/答疑正文...",
  "citation": ["教材第5章第1节"]
}}"""


class TaAgent(BaseAgent):
    """助教：可检索知识库；不可发起授课；不可中断课堂。"""

    output_model = TaMessage
    fallback_content = "助教批改生成异常，请重试。"

    def __init__(self, llm, retriever=None):
        super().__init__(llm, speaker="ta")
        self._retriever = retriever

    async def respond(self, target: str) -> dict:
        """针对作业/问题生成批改或答疑。"""
        context = ""
        if self._retriever is not None:
            # 检索放到线程池执行，避免阻塞事件循环
            docs = await asyncio.to_thread(self._retriever.retrieve, target, 4)
            context = self._retriever.format_context(docs)

        prompt = _SYSTEM_PROMPT.format(
            target=target or "（未指定）",
            context=context or "（暂无检索内容）",
        )
        return await self.generate(prompt)
