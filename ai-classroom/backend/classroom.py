"""课堂会话管理：会话注册、LangGraph 流程驱动、WebSocket 消息推送与断线重传。"""
import asyncio
import json
import threading
import uuid
from collections import defaultdict

from llm_client import get_llm
from rag.retriever import HybridRetriever
from graph.workflow import build_workflow, create_initial_state


class MessageBuffer:
    """断线重传缓冲：配置了 REDIS_URL 用 Redis，否则使用进程内缓冲。"""

    def __init__(self, redis_url: str = ""):
        self._use_redis = bool(redis_url)
        self._redis = None
        self._memory: dict[str, list] = defaultdict(list)
        if self._use_redis:
            import redis as _redis

            self._redis = _redis.from_url(redis_url, decode_responses=True)

    async def rpush(self, key: str, value: str):
        if self._use_redis:
            await asyncio.to_thread(self._redis.rpush, key, value)
        else:
            self._memory[key].append(value)

    @staticmethod
    def _slice(items: list[str], start: int, stop: int) -> list[str]:
        """按 Redis LRANGE 语义切片：闭区间，负索引从末尾倒数。

        注意不能直接写 items[start:stop]：Python 的 list[0:-1] 会丢掉最后一个元素，
        而断线续传依赖 lrange(0, -1) 取出全部缓冲消息，否则重连时会少补发最后一条。
        """
        n = len(items)
        if n == 0:
            return []
        begin = start + n if start < 0 else start
        end = stop + n if stop < 0 else stop
        begin = max(begin, 0)
        end = min(end, n - 1)
        if begin >= n or begin > end:
            return []
        return items[begin : end + 1]

    async def lrange(self, key: str, start: int, stop: int) -> list[str]:
        if self._use_redis:
            return await asyncio.to_thread(self._redis.lrange, key, start, stop)
        return self._slice(self._memory[key], start, stop)

    async def peek_all(self, key: str) -> list[str]:
        """读取全部缓冲（不删除）：断线重连补发的快照。"""
        if self._use_redis:
            return await asyncio.to_thread(self._redis.lrange, key, 0, -1)
        return list(self._memory[key])

    async def remove_first(self, key: str, count: int):
        """删除已成功补发的前 count 条，保留补发期间新追加的消息。"""
        if count <= 0:
            return
        if self._use_redis:
            await asyncio.to_thread(self._redis.ltrim, key, count, -1)
            return
        del self._memory[key][:count]

    async def delete(self, key: str):
        if self._use_redis:
            await asyncio.to_thread(self._redis.delete, key)
        else:
            self._memory.pop(key, None)


class ClassroomSession:
    """一次课堂会话：持有状态机、WebSocket 连接与推送序号。"""

    def __init__(self, session_id: str, topic: str, llm, retriever, buffer: MessageBuffer):
        self.session_id = session_id
        self.topic = topic
        self.llm = llm
        self.retriever = retriever
        self.buffer = buffer
        self.ws = None
        # 初始课堂状态（学生群/发言队列/三层记忆）
        self.state = create_initial_state(topic, llm)
        # 编译好的 LangGraph 应用
        self.app = build_workflow(llm, retriever)
        # 已推送消息序号（前端据此断线续传）
        self.msg_seq = 0
        # 已推送的课堂消息条数（不含 alert/end 等控制帧，供 status 接口查询）
        self.message_count = 0
        self.buffer_key = f"ws:buffer:{session_id}"
        self.running = False

    async def push(self, payload: dict):
        """推送一条消息：连接可用则直接发送，否则入断线缓冲。"""
        # 同步维护展示状态（供 status 接口实时查询轮次/阶段）。
        # 只在字段非 None 时更新：student/arbiter 等节点不产出轮次时，避免用 None 覆盖已有值
        if payload.get("round") is not None:
            self.state["round"] = payload["round"]
        if payload.get("phase") is not None:
            self.state["phase"] = payload["phase"]
        if payload.get("type") == "message":
            self.message_count += 1
        self.msg_seq += 1
        await send_or_buffer(self, {**payload, "id": self.msg_seq}, self.msg_seq)

    async def run(self):
        """流式执行 LangGraph 状态机，并把节点产出的消息逐条推送给前端。"""
        self.running = True
        # 追踪最近一次已知的轮次/阶段：student/arbiter 的 update 不携带这两个字段，
        # 推送学生消息时沿用老师节点写入的值，避免前端阶段显示回退或变成 None
        cur_round = self.state.get("round") or 0
        cur_phase = self.state.get("phase")
        try:
            async for event in self.app.astream(dict(self.state), stream_mode="updates"):
                for node_name, update in event.items():
                    if update.get("round") is not None:
                        cur_round = update["round"]
                    if update.get("phase") is not None:
                        cur_phase = update["phase"]
                    # 学生消息经 arbiter 排序后出现在 messages 中，与其他角色统一推送
                    for msg in update.get("messages", []):
                        await self.push(
                            {
                                "type": "message",
                                "speaker": msg.get("speaker", ""),
                                "content": msg.get("content", ""),
                                "citation": msg.get("citation", []),
                                "phase": cur_phase,
                                "round": cur_round,
                            }
                        )
                    # 走神/超时告警推送给前端展示
                    for alert in update.get("order_alerts", []):
                        await self.push(
                            {
                                "type": "alert",
                                "alert": alert,
                                "phase": cur_phase,
                                "round": cur_round,
                            }
                        )
            # 流程结束：phase 置为 end，并推送课堂结束信号
            self.state["phase"] = "end"
            await self.push(
                {
                    "type": "end",
                    "content": "课堂流程已结束",
                    "phase": "end",
                    "round": cur_round,
                }
            )
        except Exception as e:
            # 记录课堂任务异常（create_task 不会自动打印），并推送前端，避免无反馈
            import traceback
            print(f"[classroom:{self.session_id}] 课堂运行异常: {e}\n{traceback.format_exc()}")
            try:
                await self.push(
                    {
                        "type": "error",
                        "content": f"课堂运行异常：{e}",
                        "phase": cur_phase,
                        "round": cur_round,
                    }
                )
            except Exception:
                pass
        finally:
            self.running = False


async def send_or_buffer(session: ClassroomSession, payload: dict, msg_id: int):
    """发送消息；无论发送是否成功都先写入缓冲，重连时按 last_msg_id 补发。

    为什么发送成功也要缓冲：连接刚断开时 ASGI 层的 send 可能不会立刻报错，
    消息看似"已发送"但客户端实际收不到；统一先缓冲、重连时用 last_msg_id 去重补发，
    才能做到真不丢消息。补发成功的部分会在 replay_missed 中统一裁剪。
    """
    stored = {**payload, "id": msg_id}
    await session.buffer.rpush(session.buffer_key, json.dumps(stored))

    ws = session.ws
    if ws is not None:
        try:
            await ws.send_json(stored)
        except (BrokenPipeError, RuntimeError):
            # 发送失败无需额外处理：消息已在缓冲里，等重连补发
            pass


# ============ 全局单例 ============
_llm = get_llm()
_retriever = HybridRetriever()
_buffer = MessageBuffer()

# 会话注册表：session_id -> ClassroomSession
sessions: dict[str, ClassroomSession] = {}


def _warmup_retriever():
    """后台线程预热 RAG：加载 embedding/FAISS/BM25 较耗时（约 1.3GB 模型），
    若在课堂节点的事件循环内同步加载，会阻塞 WebSocket 心跳导致连接断开，
    因此放到后台线程预加载；索引不存在时静默跳过（课堂走空检索）。"""
    _retriever.retrieve("预加载", top_k=1)


# 后端启动后立即在后台预热 RAG 检索器
threading.Thread(target=_warmup_retriever, daemon=True).start()


def create_session(topic: str) -> ClassroomSession:
    """创建一个新的课堂会话并注册。"""
    session_id = uuid.uuid4().hex[:12]
    session = ClassroomSession(session_id, topic, _llm, _retriever, _buffer)
    sessions[session_id] = session
    return session
