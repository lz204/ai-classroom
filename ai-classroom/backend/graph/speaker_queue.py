"""发言队列与仲裁器：解决多学生并发发言时的优先级排队与消息乱序问题。"""
import asyncio
import heapq
import time

from graph.state import ClassroomState


class SpeakerQueue:
    """发言队列：管理学生举手顺序，按性格优先级 + 先来先到排序。

    优先级：学霸型(expert) > 活跃型(active) > 内向型(introvert) > 跟随型(follower)。
    """

    PRIORITY_MAP = {"expert": 3, "active": 2, "introvert": 1, "follower": 0}

    def __init__(self):
        self._heap = []
        self._sequence = 0

    def __len__(self) -> int:
        # 支持 len(queue)，供仲裁器判断是否还有排队学生
        return len(self._heap)

    def enqueue(self, student_id: str, personality: str):
        """学生举手入队：priority 高的先发言，同优先级按入队先后。"""
        p = self.PRIORITY_MAP.get(personality, 0)
        self._sequence += 1
        # 元组依次比较：优先级（取负使其升序=降序）、毫秒时间戳、自增序号、学生id
        heapq.heappush(
            self._heap, (-p, int(time.time() * 1000), self._sequence, student_id)
        )

    def next_speaker(self) -> str | None:
        """弹出下一个应发言的学生 id；队列为空返回 None。"""
        if not self._heap:
            return None
        return heapq.heappop(self._heap)[3]


# 仲裁器串行化写入锁：单进程内保证并发写入共享状态不交错
_write_lock = asyncio.Lock()


async def arbiter_node(state: ClassroomState) -> dict:
    """仲裁器节点：只对 student 节点本次新增的消息（new_messages）按发言队列排序，
    再追加进共享 messages，避免消息乱序。

    注意：绝不能把 state["messages"] 全量历史取出来排序后返回，
    否则经 operator.add 会把整段历史重复追加进 State。
    """
    pending = list(state.get("new_messages", []))
    # queue 由 student 节点每轮新建后写入 memory["queue"]，消费后自然清空，无跨轮残留
    queue: SpeakerQueue = state["memory"].get("queue", SpeakerQueue())

    async with _write_lock:
        ordered = []
        while queue and pending:
            spk = queue.next_speaker()
            # 消息的 speaker 字段统一为学生 id，与队列中的 student_id 保持一致，
            # 保证队列弹出的学生能精确匹配到本次新增消息
            match_msg = next((m for m in pending if m.get("speaker") == spk), None)
            if match_msg is None:
                # 队列中该学生本次没有新消息，保留其队列位置等待下次发言
                continue
            ordered.append(match_msg)
            pending.remove(match_msg)
        # 未在队列中的消息按原序追加，不丢弃
        ordered.extend(pending)
        # messages 追加排序后的新增消息；new_messages 清空，避免下轮重复消费
        return {"messages": ordered, "new_messages": []}
