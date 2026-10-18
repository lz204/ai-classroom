# -*- coding: utf-8 -*-
"""课堂会话管理：会话注册、LangGraph 流程驱动、WebSocket 流式推送与断线重传。

P1 能力：
- 消息按 message_start / message_delta / message_end 分片流式推送（前端打字机效果）；
- 学情档案（LearningProfile）随课堂结束生成，随 WS `profile` 帧下发并持久化；
- 消息缓冲 / 会话状态 / 学情档案 / 记忆快照统一走 SessionStore（Redis 或进程内降级，带 TTL）。
"""
import asyncio
import json
import threading
import time
import uuid

from analytics import LearningProfile
from config import (
    CLASSROOM_TTL,
    REDIS_URL,
    STREAM_CHUNK_SIZE,
    STREAM_INTERVAL_SECONDS,
)
from graph.workflow import build_workflow, create_initial_state
from llm_client import get_llm
from rag.retriever import HybridRetriever
from store import SessionStore

# 单条帧发送超时（秒）：客户端半开连接时 send 可能长期挂起，超时后转为缓冲等待重连
SEND_TIMEOUT = 5.0


class ClassroomSession:
    """一次课堂会话：持有状态机、WebSocket 连接与推送序号。"""

    def __init__(self, session_id: str, topic: str, llm, retriever, store: SessionStore):
        self.session_id = session_id
        self.topic = topic
        self.llm = llm
        self.retriever = retriever
        self.store = store
        self.ws = None
        # 初始课堂状态（学生群/发言队列/三层记忆/测验/分组）
        self.state = create_initial_state(topic, llm)
        # 编译好的 LangGraph 应用
        self.app = build_workflow(llm, retriever)
        # 已推送帧序号（前端据此断线续传）
        self.msg_seq = 0
        # 已推送的课堂消息条数（不含 alert/profile/end 等控制帧与流式分片）
        self.message_count = 0
        self.running = False
        # 课堂流程是否已启动过（防止重复 start 把状态机再跑一遍）
        self.started = False
        self.started_at = time.monotonic()
        self.finished_at: float | None = None
        # 推送串行化锁：保证「重连补发」与「实时推送」不交错，避免消息乱序/漏补
        self._send_lock = asyncio.Lock()
        # 学情档案（课堂结束后 finalize 并随 profile 帧下发）
        self.profile: LearningProfile | None = None
        self.profile_data: dict | None = None
        # 学生 id -> 姓名（消息帧带 speaker_name，前端课前即可显示真实姓名）
        self.speaker_names = {
            s.student_id: s.name for s in self.state["memory"]["students"]
        }

    # ---------- 状态持久化 ----------
    def _meta(self) -> dict:
        """会话状态快照（供 /api/classroom/{id} 跨进程/重启查询）。"""
        return {
            "session_id": self.session_id,
            "topic": self.topic,
            "running": self.running,
            "started": self.started,
            "round": self.state.get("round"),
            "phase": self.state.get("phase"),
            "msg_count": self.msg_seq,
            "message_count": self.message_count,
            "profile_ready": self.profile_data is not None,
        }

    async def _save_meta(self):
        try:
            await self.store.save_session_meta(self.session_id, self._meta())
        except Exception:
            # 状态快照失败不影响课堂主流程（内存态仍可用）
            pass

    async def _save_memory(self):
        """把三层记忆快照写入存储（配置 Redis 时跨进程可恢复；默认 TTL）。"""
        try:
            memory = self.state["memory"].get("layered_memory")
            if memory is not None:
                await self.store.set_json(
                    f"classroom:{self.session_id}:memory", memory.to_dict()
                )
        except Exception:
            pass

    # ---------- 推送 ----------
    async def push(self, payload: dict):
        """推送一条控制帧（alert / profile / end / error 等）：在线直接发，离线入缓冲。"""
        # 同步维护展示状态（供 status 接口实时查询轮次/阶段）。
        # 只在字段非 None 时更新：student/arbiter 等节点不产出轮次时，避免用 None 覆盖已有值
        if payload.get("round") is not None:
            self.state["round"] = payload["round"]
        if payload.get("phase") is not None:
            self.state["phase"] = payload["phase"]
        self.msg_seq += 1
        await send_or_buffer(self, {**payload, "id": self.msg_seq}, self.msg_seq)
        await self._save_meta()

    async def push_message(self, payload: dict):
        """推送一条课堂消息：message_start → message_delta* → message_end 分片流式发送。

        - 一条逻辑消息共用一个自增 id，前端按 id 聚合/去重，断线时只重发未完成的消息；
        - 所有帧都会写入缓冲，重连时按 last_msg_id 过滤补发；
        - 仅在连接在线时按 STREAM_INTERVAL_SECONDS 控制打字节奏（离线时快速缓冲，重连补发不延迟）。
        """
        if payload.get("round") is not None:
            self.state["round"] = payload["round"]
        if payload.get("phase") is not None:
            self.state["phase"] = payload["phase"]
        self.message_count += 1
        self.msg_seq += 1
        mid = self.msg_seq
        content = payload.get("content") or ""
        meta = {k: v for k, v in payload.items() if k not in ("type", "content")}

        frames = [{"type": "message_start", "id": mid, **meta}]
        chunk_size = max(int(STREAM_CHUNK_SIZE), 1)
        for start in range(0, len(content), chunk_size):
            frames.append(
                {
                    "type": "message_delta",
                    "id": mid,
                    "delta": content[start : start + chunk_size],
                }
            )
        frames.append({"type": "message_end", "id": mid, **meta, "content": content})

        for frame in frames:
            await send_or_buffer(self, frame, mid)
            if self.ws is not None and STREAM_INTERVAL_SECONDS > 0:
                await asyncio.sleep(STREAM_INTERVAL_SECONDS)
        await self._save_meta()

    # ---------- 课堂主流程 ----------
    async def run(self):
        """流式执行 LangGraph 状态机，并把节点产出的消息逐条推送给前端。"""
        if self.started:
            # 已启动过：忽略重复 start，避免状态机在 round=7 后再次执行
            return
        self.started = True
        self.running = True
        # 课堂真正开始时重置计时基准：避免 REST 创建后长时间未收到 start 把等待时间算进超时
        now = time.monotonic()
        self.state["class_started_at"] = now
        self.state["stage_started_at"] = now
        await self._save_meta()
        # 追踪最近一次已知的轮次/阶段：student/arbiter 的 update 不携带这两个字段，
        # 推送学生消息时沿用老师节点写入的值，避免前端阶段显示回退或变成 None
        cur_round = self.state.get("round") or 0
        cur_phase = self.state.get("phase")
        # 学情档案：随课堂消息流增量收集，结束后 finalize
        self.profile = LearningProfile(self.topic, self.state["memory"]["students"])
        try:
            async for event in self.app.astream(dict(self.state), stream_mode="updates"):
                for node_name, update in event.items():
                    if update.get("round") is not None:
                        cur_round = update["round"]
                        # 老师每轮结束都会推进记忆（摘要/实体），落一份快照
                        await self._save_memory()
                    if update.get("phase") is not None:
                        cur_phase = update["phase"]
                    # 先记录随堂测验/作答/告警（这些不产生独立消息）
                    if update.get("quiz"):
                        self.profile.record_quiz(update["quiz"])
                    if update.get("quiz_answers"):
                        self.profile.record_answers(update["quiz_answers"])
                    for alert in update.get("order_alerts", []):
                        self.profile.record_alert(alert)
                    # 学生消息经 arbiter 排序后出现在 messages 中，与其他角色统一推送
                    for msg in update.get("messages", []):
                        self.profile.record_message(msg)
                        speaker = msg.get("speaker", "")
                        await self.push_message(
                            {
                                "type": "message",
                                "speaker": speaker,
                                "speaker_name": msg.get("speaker_name")
                                or self.speaker_names.get(speaker)
                                or speaker,
                                "content": msg.get("content", ""),
                                "citation": msg.get("citation", []),
                                "quiz": msg.get("quiz"),
                                "answer": msg.get("answer"),
                                "group": msg.get("group"),
                                "phase": cur_phase,
                                "round": cur_round,
                            }
                        )
                    # 走神/超时/整堂课超时告警推送给前端展示
                    for alert in update.get("order_alerts", []):
                        await self.push(
                            {
                                "type": "alert",
                                "alert": alert,
                                "phase": cur_phase,
                                "round": cur_round,
                            }
                        )
            # 课堂结束：先推学情档案，再推结束信号
            self.profile.set_rounds(cur_round)
            self.profile_data = self.profile.finalize()
            try:
                await self.store.save_profile(self.session_id, self.profile_data)
            except Exception:
                pass
            await self.push(
                {
                    "type": "profile",
                    "profile": self.profile_data,
                    "phase": "end",
                    "round": cur_round,
                }
            )
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
                        "content": f"课堂运行异常：{e!r}",
                        "phase": cur_phase,
                        "round": cur_round,
                    }
                )
            except Exception:
                pass
        finally:
            self.running = False
            self.finished_at = time.monotonic()
            await self._save_meta()
            try:
                await self.store.purge_expired()
            except Exception:
                pass


async def send_or_buffer(session: ClassroomSession, payload: dict, msg_id: int):
    """发送帧；无论发送是否成功都先写入缓冲，重连时按 last_msg_id 补发。

    为什么发送成功也要缓冲：连接刚断开时 ASGI 层的 send 可能不会立刻报错，
    消息看似"已发送"但客户端实际收不到；统一先缓冲、重连时用 last_msg_id 去重补发。
    缓冲整堂保留（存放于 SessionStore，默认 TTL 过期），便于多客户端/刷新后补齐历史。

    与 replay_missed 共用 session._send_lock：重连补发期间新产生的消息必须等补发结束
    再发送，否则新消息会被插到旧消息前面，既乱序又会让后续按 last_msg_id 去重时漏掉旧消息。
    """
    stored = {**payload, "id": msg_id}
    async with session._send_lock:
        await session.store.append_message(
            session.session_id, json.dumps(stored, ensure_ascii=False)
        )

        ws = session.ws
        if ws is None:
            return
        try:
            # 半开连接时 send 可能长时间挂起，超时后按发送失败处理（消息已入缓冲）
            await asyncio.wait_for(ws.send_json(stored), timeout=SEND_TIMEOUT)
        except Exception:
            # 不限定异常类型：Starlette 在连接断开时可能抛 RuntimeError / WebSocketDisconnect / OSError，
            # 发送失败都不应中断课堂——消息已在缓冲里，等重连补发
            if session.ws is ws:
                session.ws = None


# ============ 全局单例 ============
_llm = get_llm()
_retriever = HybridRetriever()
_store = SessionStore(REDIS_URL)

# 会话注册表：session_id -> ClassroomSession（本进程内活跃会话；存储层另有持久化副本）
sessions: dict[str, ClassroomSession] = {}


def _prune_sessions():
    """淘汰已结束且超过 TTL 的本地会话对象（存储层数据由 SessionStore TTL 负责）。"""
    now = time.monotonic()
    for session_id, session in list(sessions.items()):
        if session.finished_at is not None and now - session.finished_at > CLASSROOM_TTL:
            sessions.pop(session_id, None)


def _warmup_retriever():
    """后台线程预热 RAG：加载 embedding/FAISS/BM25 较耗时（约 1.3GB 模型），
    若在课堂节点的事件循环内同步加载，会阻塞 WebSocket 心跳导致连接断开，
    因此放到后台线程预加载；索引不存在时静默跳过（课堂走空检索）。"""
    _retriever.retrieve("预加载", top_k=1)


# 后端启动后立即在后台预热 RAG 检索器
threading.Thread(target=_warmup_retriever, daemon=True).start()


def create_session(topic: str) -> ClassroomSession:
    """创建一个新的课堂会话并注册。"""
    _prune_sessions()
    session_id = uuid.uuid4().hex[:12]
    session = ClassroomSession(session_id, topic, _llm, _retriever, _store)
    sessions[session_id] = session
    return session


async def load_session_meta(session_id: str) -> dict | None:
    """读取会话状态快照（本进程没有该会话时从存储层查，供跨进程/重启后 status 查询）。"""
    return await _store.load_session_meta(session_id)


async def load_profile(session_id: str) -> dict | None:
    """读取学情档案：优先本进程会话，其次存储层。"""
    session = sessions.get(session_id)
    if session is not None and session.profile_data is not None:
        return session.profile_data
    return await _store.load_profile(session_id)
