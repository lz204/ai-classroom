"""学生 Agent 群：性格差异化的学生角色，模拟提问、研讨、答题。"""
from pydantic import BaseModel

from agents.base import BaseAgent

# 性格 -> 人设描述（在 System Prompt 中约束行为）
_PERSONALITY_DESC = {
    "introvert": "内向型：提问谨慎、需要引导，较少主动发言，发言简短。",
    "active": "活跃型：频繁提问、积极参与，偶尔跑题但思路发散。",
    "expert": "学霸型：理解深入，擅长深度追问与拓展，发言条理清晰。",
    "follower": "跟随型：倾向于模仿其他同学提问，从众发言，内容较为简单。",
}


class StudentMessage(BaseModel):
    """学生消息结构：speaker 为学生 id（与发言队列保持一致）"""

    speaker: str
    content: str


_SYSTEM_PROMPT = """你是一位【学生】。
【学生ID】{student_id}
【学生姓名】{student_name}
【性格】{personality_desc}
【课堂提问/话题】
{topic}
约束：
1. 只围绕当前课堂话题发言，不输出与课堂无关的内容；
2. 按你的性格特点自然提问或回答，内容精炼。
输出格式必须严格遵循以下 JSON：
{{
  "speaker": "{student_id}",
  "content": "你的发言..."
}}"""


class StudentAgent(BaseAgent):
    """学生：只能被动响应或举手发言，不可主动控场（由调度与提示词双重约束）。"""

    output_model = StudentMessage
    fallback_content = "（学生发言生成异常）"

    def __init__(self, llm, student_id: str, name: str, personality: str):
        super().__init__(llm, speaker=student_id)
        self.student_id = student_id
        self.name = name
        self.personality = personality

    async def respond(self, topic: str) -> dict:
        """针对当前话题/提问生成发言。"""
        prompt = _SYSTEM_PROMPT.format(
            student_id=self.student_id,
            student_name=self.name,
            personality_desc=_PERSONALITY_DESC.get(self.personality, _PERSONALITY_DESC["follower"]),
            topic=topic or "（老师正在讲解，请认真听讲并适时提问）",
        )
        return await self.generate(prompt)
