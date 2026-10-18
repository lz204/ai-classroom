# -*- coding: utf-8 -*-
"""P1 存储层单测：SessionStore（Redis 降级 / TTL / Key 命名）+ LayeredMemory 序列化。

运行方式（backend/ 目录下）：
    python tests/test_store.py

测试函数均为同步函数（内部 asyncio.run），因此也能被 pytest 直接收集。
无需 redis-server / 网络 / 真实 LLM：Redis 分支用失败替身验证降级路径，
TTL 用可控假时钟（替换 store._now），不真实 sleep。
"""
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

# 测试隔离：不连 Redis、不加载真实向量索引
os.environ["REDIS_URL"] = ""
os.environ["VECTOR_INDEX_PATH"] = str(BACKEND_DIR / "_no_index_for_tests")

import config  # noqa: E402
from store import (  # noqa: E402
    KIND_MESSAGES,
    KIND_META,
    KIND_PROFILE,
    SessionStore,
    session_key,
)
from memory.layered_memory import LayeredMemory, _SimpleEntityStore  # noqa: E402


class FakeClock:
    """可控单调时钟：替换 store._now，TTL 测试无需真实 sleep。"""

    def __init__(self, start: float = 1000.0):
        self.now = start

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_store(default_ttl: int | None = None) -> tuple[SessionStore, FakeClock]:
    """创建一个使用假时钟的进程内 store（不连接任何 Redis）。"""
    clock = FakeClock()
    store = SessionStore("")
    store._now = clock.monotonic
    if default_ttl is not None:
        store._default_ttl = default_ttl
    return store, clock


# ============ 通用 JSON CRUD ============


async def _test_generic_json_crud():
    store = SessionStore("")
    assert store._use_redis is False, "未配置 REDIS_URL 时应直接使用进程内实现"
    assert store._redis is None

    key = "classroom:demo:custom"
    assert await store.get_json(key) is None

    value = {"status": "running", "round": 1, "中文": "值", "nested": {"ok": True}}
    await store.set_json(key, value)
    assert await store.get_json(key) == value

    # 覆盖写
    await store.set_json(key, {"overwritten": 1})
    assert await store.get_json(key) == {"overwritten": 1}

    # 删除后不可读；重复删除不报错
    await store.delete(key)
    assert await store.get_json(key) is None
    await store.delete(key)


# ============ 会话元数据 / 学情档案 ============


async def _test_session_meta_profile():
    store, _ = make_store()
    sid = "abc123"

    # Key 命名契约：classroom:{session_id}:{meta|profile|messages}
    assert session_key(sid, KIND_META) == f"classroom:{sid}:meta"
    assert session_key(sid, KIND_PROFILE) == f"classroom:{sid}:profile"
    assert session_key(sid, KIND_MESSAGES) == f"classroom:{sid}:messages"

    assert await store.load_session_meta(sid) is None
    assert await store.load_profile(sid) is None

    meta = {"status": "running", "phase": "teaching", "round": 1, "message_count": 0}
    await store.save_session_meta(sid, meta)
    assert await store.load_session_meta(sid) == meta
    assert session_key(sid, KIND_META) in store._memory, "meta 必须写在统一前缀的 key 上"

    profile = {"topic": "极限与连续", "students": [{"id": "student_1", "messages": 3}]}
    await store.save_profile(sid, profile)
    assert await store.load_profile(sid) == profile
    assert session_key(sid, KIND_PROFILE) in store._memory

    # 覆盖更新（课堂结束后写终态）
    await store.save_session_meta(
        sid, {**meta, "status": "ended", "phase": "end", "round": 7}
    )
    ended = await store.load_session_meta(sid)
    assert ended["status"] == "ended" and ended["round"] == 7

    # 会话间互相隔离
    assert await store.load_session_meta("other") is None
    assert await store.load_profile("other") is None


# ============ 消息缓冲 ============


async def _test_append_list_messages():
    store, _ = make_store()
    sid = "order-session"
    assert await store.list_messages(sid) == []

    payloads = [
        json.dumps({"type": "message", "id": i, "content": f"m{i}"}, ensure_ascii=False)
        for i in range(1, 6)
    ]
    for payload in payloads:
        await store.append_message(sid, payload)

    messages = await store.list_messages(sid)
    assert messages == payloads, "消息必须按追加顺序保存"
    assert session_key(sid, KIND_MESSAGES) in store._memory

    # 返回副本：调用方修改不影响存储
    messages.append("x")
    messages[0] = "changed"
    assert await store.list_messages(sid) == payloads

    # 会话隔离
    assert await store.list_messages("other") == []

    # 容错：直接传 dict/list 时自动 JSON 序列化
    await store.append_message(sid, {"type": "alert", "id": 99})
    assert json.loads((await store.list_messages(sid))[-1]) == {"type": "alert", "id": 99}


# ============ 清理单个会话 ============


async def _test_clear_session():
    store, _ = make_store()
    sid = "clear-session"
    await store.save_session_meta(sid, {"status": "running"})
    await store.save_profile(sid, {"topic": "t"})
    await store.append_message(sid, json.dumps({"id": 1}))

    # 另一个会话不应被清理
    await store.save_session_meta("keep", {"status": "running"})
    await store.append_message("keep", json.dumps({"id": 2}))

    await store.clear_session(sid)
    assert await store.load_session_meta(sid) is None
    assert await store.load_profile(sid) is None
    assert await store.list_messages(sid) == []

    assert await store.load_session_meta("keep") == {"status": "running"}
    assert await store.list_messages("keep") == [json.dumps({"id": 2})]


# ============ TTL 过期 ============


async def _test_ttl_expiry():
    store, clock = make_store()

    # 默认 TTL 取 config.CLASSROOM_TTL
    assert store._default_ttl == config.CLASSROOM_TTL
    assert config.CLASSROOM_TTL > 0

    # 显式 TTL：到期前可读，到期后读 None，并惰性删除内部条目
    key = "classroom:ttl:generic"
    await store.set_json(key, {"v": 1}, ttl=10)
    clock.advance(9)
    assert await store.get_json(key) == {"v": 1}
    clock.advance(2)  # 累计 11s > 10s
    assert await store.get_json(key) is None
    assert key not in store._memory, "过期条目应被惰性删除，不能泄漏内存"

    # 不传 ttl 时使用默认 CLASSROOM_TTL
    await store.save_session_meta("ttl-session", {"status": "running"})
    clock.advance(config.CLASSROOM_TTL - 1)
    assert await store.load_session_meta("ttl-session") is not None
    clock.advance(2)
    assert await store.load_session_meta("ttl-session") is None

    # ttl=0 视为不过期（供不需要过期的元数据使用）
    await store.set_json("classroom:ttl:forever", {"v": 2}, ttl=0)
    clock.advance(config.CLASSROOM_TTL * 10)
    assert await store.get_json("classroom:ttl:forever") == {"v": 2}


async def _test_append_refreshes_ttl():
    store, clock = make_store(default_ttl=10)
    sid = "refresh-session"
    await store.save_session_meta(sid, {"status": "running"})
    await store.save_profile(sid, {"topic": "t"})
    await store.append_message(sid, "m1")  # t=0，过期时间 10

    clock.advance(9)
    assert await store.list_messages(sid) == ["m1"]

    await store.append_message(sid, "m2")  # t=9，该会话续期到 t=19
    clock.advance(9)  # t=18：若未续期，t=10 时已过期
    assert await store.list_messages(sid) == ["m1", "m2"], "append_message 后必须刷新 messages TTL"
    assert await store.load_session_meta(sid) == {"status": "running"}, "活跃会话 meta 应一并续期"
    assert await store.load_profile(sid) == {"topic": "t"}, "活跃会话 profile 应一并续期"

    # 停止追加后统一过期
    clock.advance(2)  # t=20 > 19
    assert await store.list_messages(sid) == []
    assert await store.load_session_meta(sid) is None
    assert await store.load_profile(sid) is None

    # 没有 meta/profile 时 append 也不应报错
    await store.append_message("only-messages", "m1")
    assert await store.list_messages("only-messages") == ["m1"]


async def _test_purge_expired():
    store, clock = make_store()
    await store.set_json("classroom:p:a", {"v": 1}, ttl=5)
    await store.set_json("classroom:p:b", {"v": 2}, ttl=5)
    await store.save_session_meta("pmeta", {"s": 1}, ttl=5)
    await store.append_message("pmsgs", "m1")  # 默认 TTL，不过期
    await store.set_json("classroom:p:keep", {"v": 3}, ttl=100)

    clock.advance(6)
    removed = await store.purge_expired()
    assert removed == 3, f"应清理 3 条过期条目，实际 {removed}"
    assert await store.get_json("classroom:p:a") is None
    assert await store.load_session_meta("pmeta") is None
    assert await store.get_json("classroom:p:keep") == {"v": 3}
    assert await store.list_messages("pmsgs") == ["m1"]

    # 无过期条目时返回 0
    assert await store.purge_expired() == 0


# ============ Redis=空 / Redis 连接失败降级 ============


async def _test_redis_empty_falls_back_to_memory():
    store = SessionStore()  # 默认 redis_url=""
    assert store._use_redis is False
    await store.save_session_meta("fallback", {"status": "running"})
    assert await store.load_session_meta("fallback") == {"status": "running"}
    await store.append_message("fallback", "m1")
    assert await store.list_messages("fallback") == ["m1"]


class _FailingRedis:
    """模拟「配置了 REDIS_URL 但 Redis 没起 / 连不上」的同步客户端：所有操作抛 ConnectionError。"""

    def _fail(self, *args, **kwargs):
        raise ConnectionError("Connection refused")

    set = get = delete = lrange = expire = _fail

    def rpush(self, *args, **kwargs):
        self._fail()

    def pipeline(self, *args, **kwargs):
        return self


async def _test_redis_failure_degrades():
    store = SessionStore("")
    # 模拟 Redis 分支遇到连接异常（不依赖真实网络，保证无 redis-server 也能测）
    store._use_redis = True
    store._redis = _FailingRedis()

    records: list[logging.LogRecord] = []

    class _Handler(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = _Handler()
    store_logger = logging.getLogger("store")
    store_logger.addHandler(handler)
    try:
        # 写入失败：自动降级并把数据写进进程内，服务不崩溃
        await store.set_json("classroom:deg:meta", {"a": 1}, ttl=10)
        assert store._use_redis is False, "Redis 连接异常后必须降级为进程内实现"
        assert store._redis is None
        assert records and records[0].levelno == logging.WARNING, "降级时必须打 warning 日志"
        assert "降级" in records[0].getMessage()
        assert await store.get_json("classroom:deg:meta") == {"a": 1}

        # 降级后所有方法正常
        await store.save_session_meta("deg", {"status": "running"})
        await store.append_message("deg", "m1")
        assert await store.list_messages("deg") == ["m1"]
        assert await store.load_session_meta("deg") == {"status": "running"}
        await store.clear_session("deg")
        assert await store.list_messages("deg") == []
        assert await store.purge_expired() == 0

        # 读失败语义：返回 None / []，不向上抛异常，并触发降级
        store2 = SessionStore("")
        store2._use_redis = True
        store2._redis = _FailingRedis()
        assert await store2.get_json("classroom:deg:missing") is None
        assert store2._use_redis is False
        store3 = SessionStore("")
        store3._use_redis = True
        store3._redis = _FailingRedis()
        assert await store3.list_messages("deg") == []
        assert store3._use_redis is False
    finally:
        store_logger.removeHandler(handler)


# ============ Redis 成功路径（用替身验证命令 / key / TTL，无需 redis-server） ============


class _FakePipeline:
    """redis-py pipeline 替身：缓存命令，execute 时按序作用到 _FakeRedis。"""

    def __init__(self, redis: "_FakeRedis"):
        self._redis = redis
        self._ops: list[tuple] = []

    def rpush(self, key, value):
        self._ops.append(("rpush", key, value))
        return self

    def expire(self, key, ttl):
        self._ops.append(("expire", key, ttl))
        return self

    def execute(self):
        results = []
        for name, key, arg in self._ops:
            results.append(getattr(self._redis, name)(key, arg))
        self._ops = []
        return results


class _FakeRedis:
    """极简 redis-py 替身：只实现 SessionStore 用到的命令，用于验证 Redis 分支行为。"""

    def __init__(self):
        self.strings: dict[str, str] = {}
        self.lists: dict[str, list[str]] = {}
        self.expires: dict[str, int] = {}

    def set(self, key, value, ex=None):
        self.strings[key] = value
        if ex is not None:
            self.expires[key] = ex

    def get(self, key):
        return self.strings.get(key)

    def delete(self, *keys):
        for key in keys:
            self.strings.pop(key, None)
            self.lists.pop(key, None)
            self.expires.pop(key, None)
        return len(keys)

    def rpush(self, key, value):
        self.lists.setdefault(key, []).append(value)
        return len(self.lists[key])

    def lrange(self, key, start, stop):
        items = self.lists.get(key, [])
        if stop == -1:
            return list(items)
        return list(items[start : stop + 1])

    def expire(self, key, ttl):
        exists = key in self.strings or key in self.lists
        if exists:
            self.expires[key] = ttl
        return exists

    def pipeline(self):
        return _FakePipeline(self)


async def _test_redis_success_path():
    fake = _FakeRedis()
    store = SessionStore("")
    store._use_redis = True
    store._redis = fake
    store._default_ttl = 123

    # 通用 JSON：SET key value EX ttl
    await store.set_json("classroom:redis:custom", {"a": 1, "中文": "值"})
    assert fake.strings["classroom:redis:custom"] == json.dumps(
        {"a": 1, "中文": "值"}, ensure_ascii=False
    )
    assert fake.expires["classroom:redis:custom"] == 123
    assert await store.get_json("classroom:redis:custom") == {"a": 1, "中文": "值"}

    # ttl=0：不带 EX（不过期）
    await store.set_json("classroom:redis:forever", {"v": 2}, ttl=0)
    assert "classroom:redis:forever" in fake.strings
    assert "classroom:redis:forever" not in fake.expires

    # 会话 meta/profile：统一 key 前缀
    await store.save_session_meta("r1", {"status": "running"})
    await store.save_profile("r1", {"topic": "t"})
    assert "classroom:r1:meta" in fake.strings
    assert "classroom:r1:profile" in fake.strings
    assert await store.load_session_meta("r1") == {"status": "running"}
    assert await store.load_profile("r1") == {"topic": "t"}

    # 消息：RPUSH 有序保存，并刷新 messages/meta/profile 三个 key 的 TTL
    await store.append_message("r1", "m1")
    await store.append_message("r1", "m2")
    assert fake.lists["classroom:r1:messages"] == ["m1", "m2"]
    assert fake.expires["classroom:r1:messages"] == 123
    assert fake.expires["classroom:r1:meta"] == 123, "append_message 应刷新 meta TTL"
    assert fake.expires["classroom:r1:profile"] == 123, "append_message 应刷新 profile TTL"

    messages = await store.list_messages("r1")
    assert messages == ["m1", "m2"]
    messages.append("x")  # 返回副本
    assert await store.list_messages("r1") == ["m1", "m2"]

    # 通用 delete
    await store.delete("classroom:redis:custom")
    assert "classroom:redis:custom" not in fake.strings

    # 清理单会话：三个 key 全部删除
    await store.clear_session("r1")
    assert "classroom:r1:meta" not in fake.strings
    assert "classroom:r1:profile" not in fake.strings
    assert "classroom:r1:messages" not in fake.lists

    # Redis 靠 TTL 自动过期，purge_expired 返回 0；且不应触发降级
    assert await store.purge_expired() == 0
    assert store._use_redis is True


# ============ LayeredMemory 序列化 ============


async def _test_layered_memory_serialization():
    memory = LayeredMemory(llm=None)
    memory.save_context("第3章 极限的定义是什么？", "极限描述的是无限逼近的过程。")
    memory.save_context("洛必达法则怎么用？", "遇到 0/0 型未定式可对分子分母分别求导。")

    data = memory.to_dict()
    assert set(data) == {"buffer", "summary", "entities"}
    assert isinstance(data["entities"], dict)
    # SessionStore 持久化的前提：快照必须 JSON 可序列化
    text = json.dumps(data, ensure_ascii=False)

    restored = LayeredMemory.from_dict(json.loads(text), None)
    assert restored._buffer == memory._buffer
    assert restored._summary_text == memory._summary_text
    assert restored._entity_store._entities == memory._entity_store._entities
    assert restored._window_size == 10 and restored._summary_threshold == 20
    query = {"input": "第3章 极限"}
    assert restored.load_memory_variables(query) == memory.load_memory_variables(query)

    # to_dict 返回副本：修改快照不影响记忆本身
    snapshot = memory.to_dict()
    snapshot["buffer"].append({"user": "x", "ai": "y"})
    snapshot["entities"].setdefault("第3章", []).append("hack")
    assert len(memory._buffer) == 2

    # 空 / 非法字段快照不抛异常
    empty = LayeredMemory.from_dict({}, None)
    assert empty.load_memory_variables({"input": "x"}) == {
        "history": "",
        "summary": "",
        "entities": [],
    }
    empty2 = LayeredMemory.from_dict({"buffer": None, "summary": None, "entities": []}, None)
    assert empty2.to_dict() == {"buffer": [], "summary": "", "entities": {}}

    # _SimpleEntityStore 独立序列化
    entity_store = _SimpleEntityStore()
    entity_store.add("极限", "Q: 极限是什么 A: 无限逼近")
    assert _SimpleEntityStore.from_dict(entity_store.to_dict()).recall(["极限"]) == [
        "Q: 极限是什么 A: 无限逼近"
    ]

    # 与 SessionStore 组合：记忆快照可持久化并恢复（Lead 接入会话层后的使用方式）
    store, _ = make_store()
    await store.set_json("classroom:mem:snapshot", data)
    loaded = await store.get_json("classroom:mem:snapshot")
    assert LayeredMemory.from_dict(loaded, None).to_dict() == data


# ============ 测试入口 ============


def test_generic_json_crud():
    asyncio.run(_test_generic_json_crud())


def test_session_meta_profile():
    asyncio.run(_test_session_meta_profile())


def test_append_list_messages():
    asyncio.run(_test_append_list_messages())


def test_clear_session():
    asyncio.run(_test_clear_session())


def test_ttl_expiry():
    asyncio.run(_test_ttl_expiry())


def test_append_refreshes_ttl():
    asyncio.run(_test_append_refreshes_ttl())


def test_purge_expired():
    asyncio.run(_test_purge_expired())


def test_redis_empty_falls_back_to_memory():
    asyncio.run(_test_redis_empty_falls_back_to_memory())


def test_redis_failure_degrades():
    asyncio.run(_test_redis_failure_degrades())


def test_redis_success_path():
    asyncio.run(_test_redis_success_path())


def test_layered_memory_serialization():
    asyncio.run(_test_layered_memory_serialization())


def _run_all() -> None:
    tests = [
        test_generic_json_crud,
        test_session_meta_profile,
        test_append_list_messages,
        test_clear_session,
        test_ttl_expiry,
        test_append_refreshes_ttl,
        test_purge_expired,
        test_redis_empty_falls_back_to_memory,
        test_redis_failure_degrades,
        test_redis_success_path,
        test_layered_memory_serialization,
    ]
    for index, test in enumerate(tests, start=1):
        test()
        print(f"[{index}/{len(tests)}] {test.__name__} PASS")
    print("\nALL P1 STORE TESTS PASSED")


if __name__ == "__main__":
    _run_all()
