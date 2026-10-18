"""主讲老师 Agent：授课内容生成、知识点讲解、随堂提问设计。"""
import asyncio

from pydantic import BaseModel, Field, field_validator

from agents.base import BaseAgent


class TeacherMessage(BaseModel):
    """老师消息结构（对应项目文档 6.1.5）"""

    speaker: str
    content: str
    citation: list[str] = Field(default_factory=list)
    action: str | None = None

    @field_validator("citation", mode="before")
    @classmethod
    def _citation_none_to_empty(cls, v):
        """容忍 LLM 在无引用时输出 citation=null，统一转为空列表（否则解析失败）"""
        if v is None:
            return []
        return v


_SYSTEM_PROMPT = """你是一位严谨的【主讲老师】。
【主题】{topic}
【当前环节】{stage_desc}
【参考内容】
{context}
【记忆上下文】
{memory_text}
约束：
1. 优先基于【参考内容】讲解，并标注引用来源（citation）；
2. 若【参考内容】为空，请基于该主题的通用知识展开讲解，citation 留空，不要编造具体教材章节引用；
3. 严禁输出任何未检索到的数学公式推导，严禁回答任何与课堂无关的闲聊。
输出格式必须严格遵循以下 JSON：
{{
  "speaker": "teacher",
  "content": "授课正文...",
  "citation": ["教材第3章第2节"]
}}"""


class TeacherAgent(BaseAgent):
    """主讲老师：可调用 RAG 检索；不可批改作业；不可管控秩序（由提示词约束）。"""

    output_model = TeacherMessage
    fallback_content = "老师授课生成异常，请重试。"

    def __init__(self, llm, retriever=None):
        super().__init__(llm, speaker="teacher")
        self._retriever = retriever

    async def respond(self, topic: str, stage_desc: str, memory_text: str = "") -> dict:
        """生成一条老师消息。

        :param topic: 授课主题
        :param stage_desc: 当前环节描述（开篇讲解/答疑收束/研讨总结等）
        :param memory_text: 记忆上下文文本
        """
        # 先检索知识库（无索引/检索为空时得到空参考内容，走空检索兜底）
        # RAG 检索为 CPU/IO 密集操作，放到线程池执行，避免阻塞事件循环（阻塞会拖垮 WebSocket 心跳）
        context = ""
        if self._retriever is not None:
            docs = await asyncio.to_thread(self._retriever.retrieve, topic, 4)
            context = self._retriever.format_context(docs)

        prompt = _SYSTEM_PROMPT.format(
            topic=topic,
            stage_desc=stage_desc,
            context=context or "（暂无检索内容）",
            memory_text=memory_text or "（暂无）",
        )
        return await self.generate(prompt)
