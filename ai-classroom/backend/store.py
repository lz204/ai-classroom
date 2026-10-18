# -*- coding: utf-8 -*-
"""会话存储层：Redis 持久化 + 进程内降级 + TTL 清理。

背景：课堂会话的元数据（status/phase/round）、学情档案与消息缓冲需要跨进程/跨重启可查。
配置了 REDIS_URL 时写入 Redis；未配置 REDIS_URL 或连接失败时自动降级为当前进程内的
dict 实现，保证「没装 / 没起 Redis」的环境下服务依然可用（仅重启后数据丢失）。

实现要点：
1. Redis 使用同步客户端 redis-py，所有调用经 ``asyncio.to_thread`` 执行，不阻塞事件循环
   （与原 MessageBuffer 写法一致）；
2. Key 统一命名：``classroom:{session_id}:{meta|profile|messages}``；
3. TTL 默认取 ``config.CLASSROOM_TTL``（秒）。Redis 侧用 ``SET ... EX`` / ``EXPIRE``；
   进程内侧保存 ``(value, expire_at)``，读取/写入时惰性删除过期项；
4. 所有方法都是 async；读失败返回 ``None`` / ``[]`` 并记录日志，Redis 连接/操作异常会
   触发一次性降级告警，之后全部走进程内实现，不让服务崩溃；
5. 进程内实现额外提供 ``purge_expired()`` 主动清理过期条目；Redis 侧由 Redis TTL 自动过期。

会话层集成约定：
- ``save_session_meta`` 存状态快照（status/phase/round/message_count 等），供
  ``/api/classroom/{session_id}`` 跨进程/重启查询；
- ``append_message`` 传入已 ``json.dumps`` 好的帧字符串，``list_messages`` 按追加顺序
  返回原始字符串（可据此按 ``msg_id`` 过滤做断线补发）；
- ``clear_session`` 删除 meta/profile/messages 三个 key，课堂结束/取消时调用；
- 通用 ``set_json/get_json/delete`` 的 key 由调用方自行指定（建议同样带 classroom: 前缀）。
"""
import asyncio
import json
import logging
import time
from typing import Any, Callable

import config

logger = logging.getLogger(__name__)

# Key 前缀与分类：跨进程可直接按前缀查询/清理
KEY_PREFIX = "classroom"
KIND_META = "meta"
KIND_PROFILE = "profile"
KIND_MESSAGES = "messages"
_SESSION_KINDS = (KIND_META, KIND_PROFILE, KIND_MESSAGES)

# Redis 连接/读写超时（秒）：Redis 挂掉时快速失败并降级，避免 asyncio.to_thread 线程长时间挂起
REDIS_TIMEOUT_SECONDS = 5.0


def session_key(session_id: str, kind: str) -> str:
    """会话相关 key 的统一命名：``classroom:{session_id}:{kind}``。"""
    return f"{KEY_PREFIX}:{session_id}:{kind}"


class _Entry:
    """进程内存储条目：value 为 JSON 字符串（或消息列表），expire_at 为 None 表示不过期。"""

    __slots__ = ("value", "expire_at")

    def __init__(self, value: Any, expire_at: float | None):
        self.value = value
        self.expire_at = expire_at


class SessionStore:
    """会话存储：优先 Redis，连接失败/未配置时进程内降级。

    使用方式（会话层）::

        store = SessionStore(config.REDIS_URL)
        await store.save_session_meta(session_id, {"status": "running", ...})
        await store.append_message(session_id, json.dumps(frame, ensure_ascii=False))
    """

    def __init__(self, redis_url: str = ""):
        # 默认 TTL（秒）：config.CLASSROOM_TTL；<=0 视为不设过期
        self._default_ttl = int(config.CLASSROOM_TTL)
        # 进程内降级存储：key -> _Entry
        self._memory: dict[str, _Entry] = {}
        # 测试钩子：可替换为假时钟（如 ``store._now = clock.monotonic``），避免 TTL 测试 sleep
        self._now: Callable[[], float] = time.monotonic
        self._redis = None
        self._use_redis = False
        if redis_url:
            try:
                import redis as _redis

                self._redis = _redis.from_url(
                    redis_url,
                    decode_responses=True,
                    # 快速失败：Redis 不可达时不让线程/请求长时间挂起
                    socket_connect_timeout=REDIS_TIMEOUT_SECONDS,
                    socket_timeout=REDIS_TIMEOUT_SECONDS,
                )
                self._use_redis = True
            except Exception as exc:  # redis 未安装 / URL 非法等：直接降级，不影响服务启动
                self._degrade(exc, "初始化 Redis 客户端")

    # ============ Redis 降级 ============

    def _degrade(self, exc: Exception, where: str = "Redis 操作") -> None:
        """关闭 Redis 分支并告警；之后所有操作自动走进程内实现（只触发一次）。"""
        self._use_redis = False
        redis_client, self._redis = self._redis, None
        if redis_client is not None:
            try:  # 关闭连接池（失败也无所谓，已降级）
                redis_client.close()
            except Exception:
                pass
        logger.warning(
            "[store] %s失败（%s: %s），已降级为进程内存储",
            where,
            type(exc).__name__,
            exc,
        )

    # ============ TTL 与 key 辅助 ============

    def _resolve_ttl(self, ttl: int | None) -> int | None:
        """解析 TTL：None 用默认 TTL；<=0 表示不设过期（返回 None 便于 redis-py 省略 EX）。"""
        seconds = self._default_ttl if ttl is None else int(ttl)
        return seconds if seconds > 0 else None

    def _decode_json(self, raw: Any, key: str) -> Any:
        """把存储中的 JSON 字符串解析为 Python 对象；非法 JSON 按读失败处理。"""
        if raw is None:
            return None
        if isinstance(raw, bytes):  # 防御 decode_responses 未生效的客户端
            raw = raw.decode("utf-8", errors="replace")
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            logger.warning("[store] key=%s 的值不是合法 JSON，按读失败处理", key)
            return None

    # ============ 进程内实现 ============

    def _mem_get(self, key: str) -> _Entry | None:
        """读取进程内条目；已过期则惰性删除并返回 None。"""
        entry = self._memory.get(key)
        if entry is None:
            return None
        if entry.expire_at is not None and entry.expire_at <= self._now():
            self._memory.pop(key, None)
            return None
        return entry

    def _mem_set(self, key: str, value: Any, ttl: int | None) -> None:
        expire_at = self._now() + ttl if ttl else None
        self._memory[key] = _Entry(value, expire_at)

    def _mem_delete(self, key: str) -> None:
        self._memory.pop(key, None)

    def _mem_refresh_session(self, session_id: str, ttl: int | None) -> None:
        """活跃会话续期：meta/profile 若存在，则把过期时间刷新到 now+ttl。"""
        if not ttl:
            return
        expire_at = self._now() + ttl
        for kind in (KIND_META, KIND_PROFILE):
            entry = self._mem_get(session_key(session_id, kind))
            if entry is not None:
                entry.expire_at = expire_at

    def _mem_append(self, session_id: str, key: str, payload: str, ttl: int | None) -> None:
        """进程内追加消息：保持插入顺序，并刷新该会话 TTL。"""
        entry = self._mem_get(key)
        if entry is None or not isinstance(entry.value, list):
            entry = _Entry([], None)
            self._memory[key] = entry
        entry.value.append(payload)
        entry.expire_at = self._now() + ttl if ttl else None
        self._mem_refresh_session(session_id, ttl)

    # ============ Redis 同步辅助（经 asyncio.to_thread 调用） ============

    def _redis_set_sync(self, key: str, value: str, ttl: int | None) -> None:
        if ttl:
            self._redis.set(key, value, ex=ttl)
        else:
            self._redis.set(key, value)

    def _redis_append_sync(
        self, session_id: str, key: str, payload: str, ttl: int | None
    ) -> None:
        """RPUSH 一条消息并刷新该会话 TTL（一次 pipeline 往返，减少网络开销）。"""
        pipe = self._redis.pipeline()
        pipe.rpush(key, payload)
        if ttl:
            pipe.expire(key, ttl)
            # 活跃会话：meta/profile 一并续期；key 不存在时 EXPIRE 为 no-op
            pipe.expire(session_key(session_id, KIND_META), ttl)
            pipe.expire(session_key(session_id, KIND_PROFILE), ttl)
        pipe.execute()

    # ============ 通用 JSON ============

    async def set_json(self, key: str, value: dict, ttl: int | None = None) -> None:
        """写入任意 JSON 值（key 由调用方指定；ttl=None 用默认 TTL，ttl<=0 不过期）。"""
        text = json.dumps(value, ensure_ascii=False)
        effective_ttl = self._resolve_ttl(ttl)
        if self._use_redis:
            try:
                await asyncio.to_thread(self._redis_set_sync, key, text, effective_ttl)
                return
            except Exception as exc:
                self._degrade(exc, "Redis SET")
        self._mem_set(key, text, effective_ttl)

    async def get_json(self, key: str) -> dict | None:
        """读取 JSON 值；不存在/已过期/读失败返回 None。"""
        if self._use_redis:
            try:
                raw = await asyncio.to_thread(self._redis.get, key)
            except Exception as exc:
                self._degrade(exc, "Redis GET")
            else:
                return self._decode_json(raw, key)
        entry = self._mem_get(key)
        return self._decode_json(entry.value, key) if entry is not None else None

    async def delete(self, key: str) -> None:
        """删除任意 key（不存在不报错）。"""
        if self._use_redis:
            try:
                await asyncio.to_thread(self._redis.delete, key)
            except Exception as exc:
                self._degrade(exc, "Redis DEL")
        self._mem_delete(key)

    # ============ 会话元数据 ============

    async def save_session_meta(
        self, session_id: str, meta: dict, ttl: int | None = None
    ) -> None:
        """保存会话元数据（status/phase/round 等），跨进程/重启可查。"""
        await self.set_json(session_key(session_id, KIND_META), meta, ttl)

    async def load_session_meta(self, session_id: str) -> dict | None:
        """读取会话元数据；不存在/已过期/读失败返回 None。"""
        return await self.get_json(session_key(session_id, KIND_META))

    # ============ 学情档案 ============

    async def save_profile(
        self, session_id: str, profile: dict, ttl: int | None = None
    ) -> None:
        """保存学情档案（课堂结束 finalize 后的结构化学习报告）。"""
        await self.set_json(session_key(session_id, KIND_PROFILE), profile, ttl)

    async def load_profile(self, session_id: str) -> dict | None:
        """读取学情档案；不存在/已过期/读失败返回 None。"""
        return await self.get_json(session_key(session_id, KIND_PROFILE))

    # ============ 消息缓冲 ============

    async def append_message(self, session_id: str, payload_json: str) -> None:
        """追加一条消息 JSON 字符串（按追加顺序保存，供断线重连顺序补发）。"""
        if not isinstance(payload_json, str):
            # 容错：调用方直接传 dict/list 时自动序列化，避免 Redis RPUSH 类型错误
            payload_json = json.dumps(payload_json, ensure_ascii=False)
        effective_ttl = self._resolve_ttl(None)
        key = session_key(session_id, KIND_MESSAGES)
        if self._use_redis:
            try:
                await asyncio.to_thread(
                    self._redis_append_sync, session_id, key, payload_json, effective_ttl
                )
                return
            except Exception as exc:
                self._degrade(exc, "Redis RPUSH")
        self._mem_append(session_id, key, payload_json, effective_ttl)

    async def list_messages(self, session_id: str) -> list[str]:
        """按追加顺序返回该会话全部消息 JSON 字符串（返回副本）；无消息/读失败返回 []。"""
        key = session_key(session_id, KIND_MESSAGES)
        if self._use_redis:
            try:
                items = await asyncio.to_thread(self._redis.lrange, key, 0, -1)
            except Exception as exc:
                self._degrade(exc, "Redis LRANGE")
            else:
                return [
                    item.decode("utf-8", errors="replace")
                    if isinstance(item, bytes)
                    else item
                    for item in items
                ]
        entry = self._mem_get(key)
        if entry is None or not isinstance(entry.value, list):
            return []
        return list(entry.value)  # 返回副本，调用方修改不影响存储

    # ============ 清理 ============

    async def clear_session(self, session_id: str) -> None:
        """清理单个会话的全部数据（meta + profile + messages）。"""
        keys = [session_key(session_id, kind) for kind in _SESSION_KINDS]
        if self._use_redis:
            try:
                await asyncio.to_thread(self._redis.delete, *keys)
            except Exception as exc:
                self._degrade(exc, "Redis DEL(session)")
        for key in keys:
            self._mem_delete(key)

    async def purge_expired(self) -> int:
        """清理进程内已过期条目并返回清理条数（Redis 侧由 TTL 自动过期，无需扫描）。"""
        now = self._now()
        expired = [
            key
            for key, entry in self._memory.items()
            if entry.expire_at is not None and entry.expire_at <= now
        ]
        for key in expired:
            self._memory.pop(key, None)
        return len(expired)
