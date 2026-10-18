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

_OPTION_LETTERS = ("A", "B", "C", "D")


class StudentMessage(BaseModel):
    """学生消息结构：speaker 为学生 id（与发言队列保持一致）"""

    speaker: str
    content: str
    # 随堂测验作答（仅答题环节有值）：{student_id, quiz_id, choice_index, choice_text}
    answer: dict | None = None


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

# r4 小组研讨时追加的小组信息（组名/讨论角度/同组成员由 workflow 传入并原样回带）
_GROUP_SECTION = """
【小组研讨】组名：{group_name}
讨论角度：{group_angle}
同组成员：{member_names}
请围绕该讨论角度，与同组成员协作完成研讨发言（可引用/补充同组同学观点）。
"""

_ANSWER_PROMPT = """你是一位【学生】。
【学生ID】{student_id}
【学生姓名】{student_name}
【性格】{personality_desc}
【答题模式】请完成随堂测验：仔细阅读题目与选项，只选择一个最合适的选项。
【题目ID】{quiz_id}
【题目】{question}
【知识点】{knowledge_point}
【选项】
{options_text}
约束：
1. 只回答本题，不输出与题目无关的内容；
2. choice_index 为所选选项下标（0=A，1=B，2=C，3=D），必须与 choice_text 一致。
输出格式必须严格遵循以下 JSON：
{{
  "speaker": "{student_id}",
  "content": "我选择 B：简要说明理由...",
  "answer": {{
    "student_id": "{student_id}",
    "choice_index": 1,
    "choice_text": "所选选项原文"
  }}
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

    async def respond(self, topic: str, group: dict | None = None) -> dict:
        """针对当前话题/提问生成发言；研讨环节携带分组信息（并原样带回消息字典）。

        :param topic: 当前课堂话题（老师最新发言）
        :param group: r4 研讨分组信息，形如
                      {"group_id","name","angle","member_ids","members"}
        """
        prompt = _SYSTEM_PROMPT.format(
            student_id=self.student_id,
            student_name=self.name,
            personality_desc=_PERSONALITY_DESC.get(self.personality, _PERSONALITY_DESC["follower"]),
            topic=topic or "（老师正在讲解，请认真听讲并适时提问）",
        )
        if group:
            member_names = "、".join(str(n) for n in (group.get("members") or [])) or "（暂无）"
            prompt += _GROUP_SECTION.format(
                group_name=group.get("name") or group.get("group_id") or "未分组",
                group_angle=group.get("angle") or "自由讨论",
                member_names=member_names,
            )

        msg = await self.generate(prompt)
        if group:
            # 分组信息原样带回消息字典，供前端展示分组徽章 / 学情统计归属
            msg = {**msg, "group": group}
        return msg

    async def answer_quiz(self, quiz: dict) -> dict:
        """完成随堂测验：返回带结构化 answer 的学生消息。

        :param quiz: {"quiz_id","question","options":[4项],"answer_index","knowledge_point"}
        :return: {"speaker","content","answer":{"student_id","quiz_id","choice_index","choice_text"}}
        """
        quiz = quiz or {}
        options = [str(opt) for opt in (quiz.get("options") or [])]
        options_text = "\n".join(
            f"{_OPTION_LETTERS[i]}. {opt}" for i, opt in enumerate(options[:4])
        )
        prompt = _ANSWER_PROMPT.format(
            student_id=self.student_id,
            student_name=self.name,
            personality_desc=_PERSONALITY_DESC.get(self.personality, _PERSONALITY_DESC["follower"]),
            quiz_id=quiz.get("quiz_id") or "",
            question=quiz.get("question") or "",
            knowledge_point=quiz.get("knowledge_point") or "",
            options_text=options_text or "A. （缺失）\nB. （缺失）\nC. （缺失）\nD. （缺失）",
        )
        msg = await self.generate(prompt)
        msg = {**msg, "answer": self._normalize_answer(msg.get("answer"), quiz, options)}
        return msg

    def _normalize_answer(self, answer, quiz: dict, options: list[str]) -> dict:
        """校验/补全 LLM 作答；解析失败或越界时兜底为 B（无选项时 A），保证流程不中断。"""
        fallback_index = 1 if len(options) > 1 else 0
        result = dict(answer) if isinstance(answer, dict) else {}
        try:
            index = int(result.get("choice_index", fallback_index))
        except (TypeError, ValueError):
            index = fallback_index
        if not 0 <= index < max(len(options), 1):
            index = fallback_index
        choice_text = result.get("choice_text")
        if not choice_text:
            choice_text = options[index] if index < len(options) else ""
        result.update(
            {
                "student_id": result.get("student_id") or self.student_id,
                "quiz_id": result.get("quiz_id") or quiz.get("quiz_id") or "",
                "choice_index": index,
                "choice_text": choice_text,
            }
        )
        return result
