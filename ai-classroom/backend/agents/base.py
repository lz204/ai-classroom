"""角色基类：统一 JSON 结构化输出、Pydantic 校验与格式错误时重调 LLM。

核心思路（与项目文档 6.1.5 一致）：
- 解析失败时不能重试解析函数本身——同一段错误文本重试结果必然还是失败；
- 正确做法是捕获解析异常后，携带"请只输出纯 JSON"的修正 Prompt 重新调用一次 LLM。
"""
import re

from pydantic import BaseModel, ValidationError


def _try_parse(model: type[BaseModel], raw: str) -> BaseModel | None:
    """尝试从 LLM 原始输出解析出对应消息模型。

    策略：
    1. 直接 model_validate_json（最快路径）；
    2. 失败则剥离 markdown 代码围栏后，用贪婪正则提取 {...} 块再解析
       （应对 LLM 加前缀/代码块包裹的情况；贪婪匹配容忍 JSON 内部嵌套花括号）；
    3. 都失败返回 None，由上层决定是否重调 LLM。
    """
    try:
        return model.model_validate_json(raw)
    except ValidationError:
        cleaned = re.sub(r"```json|```", "", raw)
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if match:
            try:
                return model.model_validate_json(match.group(0))
            except ValidationError:
                return None
        return None


class BaseAgent:
    """Agent 基类：负责调用 LLM 并保证输出可被 Pydantic 解析。"""

    # 各角色子类覆写：输出模型、兜底消息
    output_model: type[BaseModel] = BaseModel
    fallback_content: str = "生成内容异常，请重试。"

    def __init__(self, llm, speaker: str):
        self._llm = llm
        self.speaker = speaker

    async def generate(self, prompt: str) -> dict:
        """生成一条结构化消息：调用 LLM -> 解析 -> 失败重调 -> 兜底。"""
        raw = await self._llm.ainvoke(prompt)
        msg = _try_parse(self.output_model, raw)
        if msg is not None:
            return msg.model_dump()

        # 解析失败：重调一次 LLM，追加强制格式约束
        retry_prompt = (
            prompt
            + "\n\n【重要】上次返回格式错误，请只输出纯 JSON，"
              "不要带任何前缀、解释或 markdown 代码块。"
        )
        raw2 = await self._llm.ainvoke(retry_prompt)
        msg2 = _try_parse(self.output_model, raw2)
        if msg2 is not None:
            return msg2.model_dump()

        # 仍失败：降级兜底，不让前端白屏（见项目文档 6.1.5）
        return self._fallback()

    def _fallback(self) -> dict:
        """降级兜底消息"""
        return self.output_model(
            speaker=self.speaker,
            content=self.fallback_content,
        ).model_dump()
