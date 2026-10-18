# -*- coding: utf-8 -*-
"""P0 回归冒烟测试：不需要真实 API Key、不需要向量索引，几秒内可跑完。

运行方式（在 backend/ 目录下）：
    python tests/test_smoke.py

覆盖范围：
1. 断线缓冲 MessageBuffer 的 LRANGE 语义（lrange(0, -1) 必须包含最后一条消息）；
2. Mock LLM 下的完整课堂流程：7 轮老师消息、学生/助教/管理员消息，phase/round 字段完整；
3. 断线重连补发：last_msg_id 之后的消息（含最后一条）全部补发，不丢尾消息。
"""
import asyncio
import json
import os
import sys
from collections import Counter
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

# 测试隔离：不加载真实向量索引（跳过 embedding 模型，速度快且离线可跑）、不连 Redis
os.environ["VECTOR_INDEX_PATH"] = str(BACKEND_DIR / "_no_index_for_tests")
os.environ["REDIS_URL"] = ""

import llm_client  # noqa: E402

# 强制使用内置 MockLLM，避免测试消耗真实 API 额度（必须在 import classroom 之前设置）
llm_client.LLM_API_KEY = ""

import classroom  # noqa: E402
import config  # noqa: E402
from main import replay_missed  # noqa: E402


async def test_buffer_lrange():
    """MessageBuffer 内存实现的切片必须与 Redis LRANGE 语义一致（闭区间、负索引）"""
    buffer = classroom.MessageBuffer()
    for i in range(3):
        await buffer.rpush("k", str(i))

    assert await buffer.lrange("k", 0, -1) == ["0", "1", "2"], (
        "lrange(0, -1) 必须返回全部元素（含最后一条），否则断线续传会丢消息"
    )
    assert await buffer.lrange("k", 0, 0) == ["0"]
    assert await buffer.lrange("k", -2, -1) == ["1", "2"]
    assert await buffer.lrange("k", 0, -2) == ["0", "1"]
    assert await buffer.lrange("k", 5, -1) == []
    assert await buffer.lrange("k", -5, 1) == ["0", "1"]
    assert await buffer.lrange("missing", 0, -1) == []
    print("[1/7] MessageBuffer LRANGE 语义 OK")


async def test_full_flow():
    """Mock LLM 跑完整课堂状态机，校验消息数量、阶段与轮次"""
    session = classroom.create_session("高等数学 第一章 极限与连续")
    await session.run()

    payloads = [
        json.loads(item)
        for item in await session.buffer.lrange(session.buffer_key, 0, -1)
    ]
    student_count = len(config.STUDENT_PERSONALITIES)
    # 7 轮老师 + 4 轮学生并发 + 1 助教 + 1 管理员
    expected_messages = 7 + student_count * 4 + 1 + 1
    messages = [p for p in payloads if p.get("type") == "message"]
    assert len(messages) == expected_messages, f"消息数 {len(messages)} != {expected_messages}"
    assert len(payloads) == expected_messages + 1, "缓冲里应额外包含 1 条 end 事件"
    assert payloads[-1]["type"] == "end"
    assert payloads[-1]["phase"] == "end"

    # 修复前：学生/仲裁器消息的 phase/round 为 None
    missing = [p for p in messages if not p.get("phase") or p.get("round") is None]
    assert not missing, f"存在缺少 phase/round 的消息: {missing[:2]}"

    teacher = [p for p in messages if p["speaker"] == "teacher"]
    assert [p["round"] for p in teacher] == list(range(1, 8)), "老师轮次应严格 1..7"
    assert [p["phase"] for p in teacher] == [
        "teaching", "answer", "answer", "answer", "discuss", "grading", "summary",
    ], "老师消息阶段应与实际环节一致"

    student_phases = Counter(
        p["phase"] for p in messages if p["speaker"].startswith("student_")
    )
    assert student_phases == Counter(
        {"quiz": student_count * 3, "discuss": student_count}
    ), f"学生阶段分布异常: {student_phases}"

    ta = [p for p in messages if p["speaker"] == "ta"]
    assert len(ta) == 1 and ta[0]["phase"] == "grading"
    manager = [p for p in messages if p["speaker"] == "manager"]
    assert len(manager) == 1 and manager[0]["phase"] == "summary"

    # status 接口依赖的 state 也应停在结束态
    assert session.state["round"] == 7
    assert session.state["phase"] == "end"
    assert session.message_count == expected_messages
    assert not session.running

    # 重复 start 幂等：再次 run 不应把状态机再跑一遍（round 会变成 8、重复推 end）
    seq_before = session.msg_seq
    await session.run()
    assert session.msg_seq == seq_before, "重复 run 不应产生新消息"
    assert session.state["round"] == 7
    print(f"[2/7] 完整课堂流程 OK（{expected_messages} 条消息，{student_count} 名学生，重复 start 幂等）")


async def test_replay_last_message():
    """断线重连补发：last_msg_id 之后的消息（含最后一条）不能丢；补发失败时保留缓冲"""
    session = classroom.create_session("断线续传测试")
    session.msg_seq = 3
    for i in range(1, 4):
        await session.buffer.rpush(
            session.buffer_key,
            json.dumps({"type": "message", "id": i, "content": f"m{i}"}, ensure_ascii=False),
        )

    sent = []

    class FakeWebSocket:
        async def send_json(self, payload):
            sent.append(payload)

    session.ws = FakeWebSocket()
    await replay_missed(session, session.ws, 1)
    assert [p["id"] for p in sent] == [2, 3], f"补发消息不完整: {[p['id'] for p in sent]}"
    # 缓冲不再裁剪：整堂课消息保留在缓冲中，供多客户端/刷新后按 last_msg_id 补历史
    assert len(await session.buffer.lrange(session.buffer_key, 0, -1)) == 3

    # 补发途中断线：缓冲必须保留，等下次重连继续补发（用独立会话，避免与上一段缓冲叠加）
    failure_session = classroom.create_session("断线续传-补发失败")
    failure_session.msg_seq = 5
    for i in range(1, 6):
        await failure_session.buffer.rpush(
            failure_session.buffer_key,
            json.dumps({"type": "message", "id": i, "content": f"m{i}"}, ensure_ascii=False),
        )

    class BrokenWebSocket:
        async def send_json(self, payload):
            raise RuntimeError("connection closed")

    failure_session.ws = BrokenWebSocket()
    await replay_missed(failure_session, failure_session.ws, 2)
    kept = await failure_session.buffer.lrange(failure_session.buffer_key, 0, -1)
    assert len(kept) == 5, "补发失败时不能裁剪缓冲，否则消息会永久丢失"
    print("[3/7] 断线重连续传 OK（尾消息未丢失，补发失败可重试）")


async def test_replay_targets_given_ws():
    """replay 必须发给传入的「当前连接」，而不是 session.ws（双连接并发补发时不能错发）"""
    session = classroom.create_session("多连接补发测试")
    session.msg_seq = 2
    for i in range(1, 3):
        await session.buffer.rpush(
            session.buffer_key,
            json.dumps({"type": "message", "id": i, "content": f"m{i}"}, ensure_ascii=False),
        )

    sent_a, sent_b = [], []

    class FakeWebSocket:
        def __init__(self, sink):
            self._sink = sink

        async def send_json(self, payload):
            self._sink.append(payload["id"])

    # 会话当前连接是 B，但补发目标是 A：消息必须只发给 A
    session.ws = FakeWebSocket(sent_b)
    await replay_missed(session, FakeWebSocket(sent_a), 0)
    assert sent_a == [1, 2], f"A 连接应收到全部历史，实际 {sent_a}"
    assert sent_b == [], f"不能把补发消息错发到 session.ws，实际 {sent_b}"
    print("[4/7] 补发使用显式连接 OK（不会错发到其他客户端）")


async def test_replay_serialized_with_live_push():
    """重连补发期间产生的实时消息必须排在补发消息之后（不能插队导致乱序/漏补）"""
    session = classroom.create_session("乱序回归测试")
    session.msg_seq = 3
    for i in range(1, 4):
        await session.buffer.rpush(
            session.buffer_key,
            json.dumps({"type": "message", "id": i, "content": f"m{i}"}, ensure_ascii=False),
        )

    order = []

    class SlowWebSocket:
        async def send_json(self, payload):
            order.append(payload["id"])
            await asyncio.sleep(0.02)  # 模拟网络延迟，给并发 push 插队的机会

    session.ws = SlowWebSocket()

    async def push_during_replay():
        await asyncio.sleep(0.01)  # 等补发开始后再产生新消息
        await session.push({"type": "message", "content": "m4"})

    await asyncio.gather(replay_missed(session, session.ws, 0), push_during_replay())
    assert order == [1, 2, 3, 4], f"重连补发与新消息乱序: {order}"
    # 缓冲不裁剪：3 条历史 + 补发期间实时产生的 1 条都在缓冲中
    assert len(await session.buffer.lrange(session.buffer_key, 0, -1)) == 4
    print("[5/7] 重连补发与实时推送串行化 OK（不插队、不乱序）")


async def test_send_failure_does_not_abort_class():
    """发送时连接断开的异常类型不可枚举（RuntimeError/WebSocketDisconnect/OSError），不能中断课堂"""
    session = classroom.create_session("发送失败测试")

    class BrokenWebSocket:
        async def send_json(self, payload):
            raise OSError("client disconnected")

    session.ws = BrokenWebSocket()
    # 不应抛出任何异常：消息先入缓冲，等重连补发；同时把失效连接标记为 None
    await session.push({"type": "message", "content": "m1"})
    assert session.ws is None, "发送失败后应把失效连接置空，避免后续消息继续撞错连接"
    assert len(await session.buffer.lrange(session.buffer_key, 0, -1)) == 1
    print("[6/7] 发送异常不中止课堂 OK（消息已缓冲，连接已置空）")


async def test_order_alerts_do_not_deadlock():
    """学生全部超时产生 order_alerts 时，路由应跳转 manager 并在处理后清空，不能死循环"""
    import agents.student as student_module

    original = student_module.StudentAgent.respond

    async def always_timeout(self, topic):
        raise asyncio.TimeoutError("模拟学生 Agent 超时")

    student_module.StudentAgent.respond = always_timeout
    try:
        session = classroom.create_session("告警路由测试")
        await session.run()
    finally:
        student_module.StudentAgent.respond = original

    payloads = [
        json.loads(item)
        for item in await session.buffer.lrange(session.buffer_key, 0, -1)
    ]
    alerts = [p for p in payloads if p.get("type") == "alert"]
    messages = [p for p in payloads if p.get("type") == "message"]
    assert alerts, "学生全部超时应产生 order_alerts 告警"
    assert not session.running
    assert session.state["round"] == 7, f"告警不应打乱轮次: {session.state['round']}"
    assert session.state["phase"] == "end"
    # manager 至少出现「处理告警」与「最终学情总结」两次，且流程能正常结束（无 manager 死循环）
    manager = [p for p in messages if p["speaker"] == "manager"]
    assert len(manager) >= 2, f"manager 应至少推送 2 条，实际 {len(manager)}"
    print(f"[7/7] order_alerts 路由 OK（{len(alerts)} 条告警，manager {len(manager)} 条，流程正常结束）")


async def _run_all():
    await test_buffer_lrange()
    await test_full_flow()
    await test_replay_last_message()
    await test_replay_targets_given_ws()
    await test_replay_serialized_with_live_push()
    await test_send_failure_does_not_abort_class()
    await test_order_alerts_do_not_deadlock()
    print("\nALL P0 REGRESSION TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(_run_all())
