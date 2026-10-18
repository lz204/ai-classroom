# -*- coding: utf-8 -*-
"""学情档案：从课堂消息流中提炼结构化学习报告（不依赖 LLM，纯规则统计）。

由 ClassroomSession.run 在课堂结束后调用 finalize() 生成，随 WS `profile` 帧下发，
同时写入 SessionStore（配置 Redis 时持久化）。
"""
import time


class LearningProfile:
    """单堂课学情档案收集器。

    统计口径：
    - 每个学生：发言数、提问数、随堂测验作答数/正确数、超时（走神）次数、所属研讨小组；
    - 课堂整体：消息数、老师/助教/管理员消息数、告警数、学生发言占比、知识点引用；
    - 随堂测验：题目、作答总数、正确率；
    - 薄弱点：测验答错的知识点 + 存在超时/走神的学生。
    """

    def __init__(self, topic: str, students: list):
        self.topic = topic
        self.rounds = 0
        self.message_count = 0
        self.teacher_messages = 0
        self.ta_messages = 0
        self.manager_messages = 0
        self.alerts: list[dict] = []
        self.quiz: dict | None = None
        self.quiz_answers: list[dict] = []
        self.knowledge_points: list[str] = []
        self._kp_seen: set[str] = set()
        # 分组元信息：group_id -> {group_id, name, angle, member_ids}
        self.group_meta: dict[str, dict] = {}
        self._started_at = time.monotonic()

        # 学生 id -> 统计（顺序保持 STUDENT_PERSONALITIES 顺序）
        self.students: dict[str, dict] = {}
        for s in students:
            self.students[s.student_id] = {
                "id": s.student_id,
                "name": s.name,
                "personality": s.personality,
                "group": None,
                "group_id": None,
                "messages": 0,
                "questions": 0,
                "answers": 0,
                "correct": 0,
                "timeouts": 0,
            }

    # ---------- 记录 ----------
    def set_rounds(self, rounds: int):
        self.rounds = rounds

    def _add_knowledge_point(self, point: str):
        point = (point or "").strip()
        if not point or point in self._kp_seen:
            return
        self._kp_seen.add(point)
        self.knowledge_points.append(point)

    def record_message(self, msg: dict):
        """记录一条课堂消息（老师/学生/助教/管理员）。"""
        self.message_count += 1
        for citation in msg.get("citation") or []:
            self._add_knowledge_point(str(citation))

        speaker = msg.get("speaker", "")
        content = msg.get("content", "") or ""
        student = self.students.get(speaker)
        if student is not None:
            student["messages"] += 1
            if "？" in content or "?" in content:
                student["questions"] += 1
            if msg.get("answer"):
                student["answers"] += 1
            group = msg.get("group") or {}
            if group:
                group_id = group.get("group_id") or group.get("name")
                student["group_id"] = group_id
                student["group"] = group.get("name") or group_id
                meta = self.group_meta.setdefault(
                    group_id,
                    {
                        "group_id": group_id,
                        "name": group.get("name") or group_id,
                        "angle": group.get("angle"),
                        "member_ids": [],
                    },
                )
                if group.get("angle"):
                    meta["angle"] = group["angle"]
                for member_id in group.get("member_ids") or []:
                    if member_id not in meta["member_ids"]:
                        meta["member_ids"].append(member_id)
        elif speaker == "teacher":
            self.teacher_messages += 1
        elif speaker == "ta":
            self.ta_messages += 1
        elif speaker == "manager":
            self.manager_messages += 1

    def record_alert(self, alert: dict):
        self.alerts.append(alert)
        if alert.get("type") == "timeout" and alert.get("student"):
            for student in self.students.values():
                if student["name"] == alert["student"]:
                    student["timeouts"] += 1

    def record_quiz(self, quiz: dict | None):
        if quiz:
            self.quiz = dict(quiz)
            if quiz.get("knowledge_point"):
                self._add_knowledge_point(str(quiz["knowledge_point"]))

    def record_answers(self, answers: list[dict]):
        for answer in answers or []:
            if answer:
                self.quiz_answers.append(dict(answer))

    # ---------- 输出 ----------
    def finalize(self) -> dict:
        quiz = self.quiz or {}
        answer_index = quiz.get("answer_index")
        correct_count = 0
        for answer in self.quiz_answers:
            student = self.students.get(answer.get("student_id", ""))
            is_correct = (
                answer_index is not None
                and answer.get("choice_index") == answer_index
            )
            if is_correct:
                correct_count += 1
                if student is not None:
                    student["correct"] += 1
        total_answers = len(self.quiz_answers)
        accuracy = round(correct_count / total_answers, 3) if total_answers else None

        student_messages = sum(s["messages"] for s in self.students.values())
        student_ratio = (
            round(student_messages / self.message_count, 3) if self.message_count else 0
        )

        weak_points: list[str] = []
        if total_answers and correct_count < total_answers:
            weak_points.append(quiz.get("knowledge_point") or "随堂测验知识点")
        if any(s["timeouts"] for s in self.students.values()):
            weak_points.append("课堂参与度（存在超时/走神）")

        groups: dict[str, dict] = {}
        for student in self.students.values():
            group_id = student.get("group_id") or student["group"]
            if not group_id:
                continue
            meta = self.group_meta.get(group_id, {})
            group = groups.setdefault(
                group_id,
                {
                    "group_id": group_id,
                    "name": meta.get("name") or student["group"] or group_id,
                    "angle": meta.get("angle"),
                    "members": [],
                    "messages": 0,
                },
            )
            group["members"].append(student["name"])
            group["messages"] += student["messages"]

        return {
            "topic": self.topic,
            "duration_seconds": round(time.monotonic() - self._started_at, 1),
            "rounds": self.rounds,
            "messages": self.message_count,
            "students": list(self.students.values()),
            "interaction": {
                "student_messages": student_messages,
                "teacher_messages": self.teacher_messages,
                "ta_messages": self.ta_messages,
                "manager_messages": self.manager_messages,
                "alerts": len(self.alerts),
                "student_ratio": student_ratio,
            },
            "quiz": {
                "question": quiz.get("question"),
                "options": quiz.get("options") or [],
                "total_answered": total_answers,
                "accuracy": accuracy,
            },
            "knowledge_points": self.knowledge_points,
            "weak_points": weak_points,
            "groups": list(groups.values()),
        }
