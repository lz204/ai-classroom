"""三层对话记忆：短期滑窗 + 长期摘要 + 实体关联。

- 滑窗记忆：保留最近 N 轮原始对话；
- 摘要压缩：超过窗口阈值时调用 LLM 压缩前半段历史，避免上下文无限膨胀；
- 实体记忆：抽取"知识点/章节号"等实体建立关联，提问时可召回前文同一知识点片段。

进程内内存实现；提供 to_dict()/from_dict() 序列化，可经 SessionStore 持久化到 Redis，
容器重启后由会话层恢复（见 store.py）。
"""
import re

# 章节号模式：第3章 / 第3.2节
_RE_CHAPTER = re.compile(r"第[0-9一二三四五六七八九十]+(?:章|节)[0-9.]*")
# 常见术语模式：XX定理 / XX公式 / XX方程 等
_RE_TERM = re.compile(r"[一-龥]{2,8}(?:定理|公式|定义|性质|原理|方法|法则|方程|函数|极限|导数|积分|矩阵|向量|概率|分布|收敛|连续)")


class _SimpleEntityStore:
    """简易实体存储：entity -> 相关对话片段列表"""

    def __init__(self):
        self._entities: dict[str, list[str]] = {}

    def add(self, entity: str, snippet: str):
        """记录实体与包含它的对话片段"""
        key = entity.strip()
        if not key:
            return
        snippets = self._entities.setdefault(key, [])
        if len(snippets) >= 20:  # 限制单实体片段数量，避免无限膨胀
            return
        snippets.append(snippet)

    def recall(self, entities: list[str]) -> list[str]:
        """按实体名召回相关片段（去重，保持顺序）"""
        result, seen = [], set()
        for ent in entities:
            for snippet in self._entities.get(ent, []):
                if snippet not in seen:
                    seen.add(snippet)
                    result.append(snippet)
        return result

    def to_dict(self) -> dict:
        """序列化为可 JSON 持久化的字典：{"entities": {entity: [snippet, ...]}}"""
        return {"entities": {key: list(snippets) for key, snippets in self._entities.items()}}

    @classmethod
    def from_dict(cls, data: dict) -> "_SimpleEntityStore":
        """从 to_dict() 快照恢复；非法/缺失字段按空实体存储处理（不抛异常）。"""
        entity_store = cls()
        entities = (data or {}).get("entities") or {}
        if isinstance(entities, dict):
            for key, snippets in entities.items():
                if not isinstance(snippets, list):
                    continue
                entity_store._entities[str(key)] = [str(snippet) for snippet in snippets]
        return entity_store


class LayeredMemory:
    """三层记忆：滑窗 + 摘要 + 实体。"""

    def __init__(self, llm, window_size: int = 10, summary_threshold: int = 20):
        self._llm = llm
        self._window_size = window_size
        self._summary_threshold = summary_threshold
        # 滑窗缓冲：元素为 {"user": ..., "ai": ...}
        self._buffer: list[dict] = []
        # 摘要文本（长期记忆）
        self._summary_text = ""
        # 实体存储
        self._entity_store = _SimpleEntityStore()
        # 压缩待执行标记（异步压缩，见 compress_if_needed）
        self._needs_compress = False

    def save_context(self, user_input: str, ai_output: str):
        """保存一轮对话：追加滑窗 + 抽取实体；超阈值时标记待压缩。"""
        self._buffer.append({"user": user_input, "ai": ai_output})
        # 抽取并记录实体（关联到本轮对话片段）
        for entity in self._extract_entities(user_input) | self._extract_entities(ai_output):
            snippet = f"Q: {user_input[-60:]} A: {ai_output[-80:]}"
            self._entity_store.add(entity, snippet)
        # 超过阈值标记压缩（具体压缩在 compress_if_needed 异步执行）
        if len(self._buffer) > self._summary_threshold:
            self._needs_compress = True

    def load_memory_variables(self, inputs: dict) -> dict:
        """按当前输入召回记忆变量：{history, summary, entities}。"""
        query = inputs.get("input", "")
        # 1. 滑窗：最近 N 轮原始对话
        history = [
            f"用户: {item['user']}\n助手: {item['ai']}"
            for item in self._buffer[-self._window_size:]
        ]
        # 2. 实体：根据 query 中出现的实体召回历史片段
        entities = self._entity_store.recall(self._extract_entities(query))
        return {
            "history": "\n".join(history),
            "summary": self._summary_text,
            "entities": entities,
        }

    def to_dict(self) -> dict:
        """序列化三层记忆为可 JSON 持久化的快照（供 SessionStore 保存）。

        返回 {"buffer": 滑窗对话列表, "summary": 摘要文本, "entities": 实体->片段列表}；
        buffer/entities 均返回副本，调用方修改不会影响记忆内部状态。
        """
        return {
            "buffer": [dict(item) for item in self._buffer],
            "summary": self._summary_text,
            "entities": self._entity_store.to_dict()["entities"],
        }

    @classmethod
    def from_dict(cls, data: dict, llm) -> "LayeredMemory":
        """从 to_dict() 快照恢复记忆；window_size/summary_threshold 使用默认值。

        非法/缺失字段按空值处理（不抛异常），保证从 Redis 恢复失败时课堂仍可继续。
        """
        memory = cls(llm)
        data = data if isinstance(data, dict) else {}
        buffer = data.get("buffer") or []
        if isinstance(buffer, list):
            for item in buffer:
                if not isinstance(item, dict):
                    continue
                memory._buffer.append(
                    {
                        "user": str(item.get("user", "") or ""),
                        "ai": str(item.get("ai", "") or ""),
                    }
                )
        summary = data.get("summary")
        memory._summary_text = "" if summary is None else str(summary)
        entities = data.get("entities")
        if isinstance(entities, dict):
            memory._entity_store = _SimpleEntityStore.from_dict({"entities": entities})
        # 恢复后若滑窗已超阈值，下次 compress_if_needed 仍会压缩（与 save_context 行为一致）
        memory._needs_compress = len(memory._buffer) > memory._summary_threshold
        return memory

    async def compress_if_needed(self):
        """若滑窗超阈值，调用 LLM 压缩前半段历史，并替换原始对话。"""
        if not self._needs_compress:
            return
        self._needs_compress = False
        # 压缩前半段（保留最近半段原始对话）
        split = len(self._buffer) // 2
        old_part = self._buffer[:split]
        self._buffer = self._buffer[split:]
        if not old_part:
            return
        text = "\n".join(f"用户: {it['user']}\n助手: {it['ai']}" for it in old_part)
        prompt = (
            "请用中文压缩以下课堂对话为不超过 150 字的摘要，"
            "重点保留已讲知识点与学生薄弱点：\n\n" + text
        )
        try:
            summary = await self._llm.ainvoke(prompt)
            self._summary_text = (
                (self._summary_text + "\n" + summary) if self._summary_text else summary
            )
        except Exception:
            # 压缩失败不阻塞课堂：保留原摘要即可（由上层重试装饰器保障）
            pass

    @staticmethod
    def _extract_entities(text: str) -> set[str]:
        """抽取文本中的知识点实体（章节号、术语等）"""
        entities = set(_RE_CHAPTER.findall(text))
        for m in _RE_TERM.findall(text):
            entities.add(m)
        return entities
