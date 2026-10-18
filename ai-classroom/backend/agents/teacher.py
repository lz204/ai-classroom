"""主讲老师 Agent：授课内容生成、知识点讲解、随堂测验设计。"""
import asyncio

from pydantic import BaseModel, Field, field_validator

from agents.base import BaseAgent


class QuizModel(BaseModel):
    """随堂测验结构（r2 由老师随消息一起发布）"""

    quiz_id: str
    question: str
    options: list[str]
    # 正确选项下标，必须落在 0-3（与 4 个选项一一对应）
    answer_index: int = Field(ge=0, le=3)
    knowledge_point: str = ""

    @field_validator("options")
    @classmethod
    def _options_must_be_four(cls, v: list[str]) -> list[str]:
        """契约要求恰好 4 个选项；数量不符视为解析失败，由上层重调/兜底。"""
        if len(v) != 4:
            raise ValueError("quiz.options 必须恰好包含 4 个选项")
        return v


class TeacherMessage(BaseModel):
    """老师消息结构（对应项目文档 6.1.5）"""

    speaker: str
    content: str
    citation: list[str] = Field(default_factory=list)
    action: str | None = None
    # 随堂测验（仅 r2 出题环节有值；其它环节为 None）
    quiz: QuizModel | None = None

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

# 仅在 r2 出题环节追加的约束：quiz 与 content 必须一起输出
_QUIZ_PROMPT_SECTION = """
【随堂测验】本环节除正文外，还必须同时发布一道随堂测验（quiz 与 content 一起输出），约束：
1. quiz.question 为题干；
2. quiz.options 必须恰好 4 个选项（字符串数组，按 A/B/C/D 顺序）；
3. quiz.answer_index 为正确选项下标（0/1/2/3）；
4. quiz.knowledge_point 为本题考查的知识点；
5. quiz.quiz_id 为形如 "quiz_1" 的题目编号。
输出格式必须严格遵循以下 JSON：
{{
  "speaker": "teacher",
  "content": "授课正文...",
  "citation": ["教材第3章第2节"],
  "quiz": {{
    "quiz_id": "quiz_1",
    "question": "题干...",
    "options": ["选项A内容", "选项B内容", "选项C内容", "选项D内容"],
    "answer_index": 1,
    "knowledge_point": "知识点..."
  }}
}}"""


class TeacherAgent(BaseAgent):
    """主讲老师：可调用 RAG 检索；不可批改作业；不可管控秩序（由提示词约束）。"""

    output_model = TeacherMessage
    fallback_content = "老师授课生成异常，请重试。"

    def __init__(self, llm, retriever=None):
        super().__init__(llm, speaker="teacher")
        self._retriever = retriever

    async def respond(
        self,
        topic: str,
        stage_desc: str,
        memory_text: str = "",
        with_quiz: bool = False,
    ) -> dict:
        """生成一条老师消息。

        :param topic: 授课主题
        :param stage_desc: 当前环节描述（开篇讲解/答疑收束/研讨总结等）
        :param memory_text: 记忆上下文文本
        :param with_quiz: 是否同时发布随堂测验（r2 出题环节传 True）
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
        if with_quiz:
            prompt += _QUIZ_PROMPT_SECTION
        return await self.generate(prompt)
