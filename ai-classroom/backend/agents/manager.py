"""课堂管理员 Agent：中立秩序维护者，走神提醒、超时收束、学情总结。"""
from pydantic import BaseModel

from agents.base import BaseAgent


class ManagerMessage(BaseModel):
    """管理员消息结构"""

    speaker: str
    content: str


_SYSTEM_PROMPT = """你是一位中立的【课堂管理员】。
【任务】{task}
【课堂状态摘要】
{state_summary}
约束：
1. 只负责秩序管控与学情汇总，不生成学科内容；
2. 若存在秩序告警（走神/超时），先给出提醒与收束动作；
3. 处理完告警后，在 content 末尾输出【已清空告警】标记，交由调度清空 order_alerts。
输出格式必须严格遵循以下 JSON：
{{
  "speaker": "manager",
  "content": "秩序反馈或学情报告..."
}}"""


class ManagerAgent(BaseAgent):
    """课堂管理员：可中断非秩序发言；不可生成学科内容。"""

    output_model = ManagerMessage
    fallback_content = "管理员处理异常，请重试。"

    def __init__(self, llm):
        super().__init__(llm, speaker="manager")

    async def respond(self, task: str, state_summary: str = "") -> dict:
        """处理秩序告警或输出学情总结。"""
        prompt = _SYSTEM_PROMPT.format(
            task=task,
            state_summary=state_summary or "（暂无）",
        )
        return await self.generate(prompt)
