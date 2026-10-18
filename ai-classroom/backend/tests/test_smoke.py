# -*- coding: utf-8 -*-
"""P0/P1 回归冒烟测试：不需要真实 API Key、不需要向量索引、不需要 Redis，几秒内可跑完。

运行方式（在 backend/ 目录下）：
    python tests/test_smoke.py

覆盖范围：
1. SessionStore 消息缓冲语义（追加顺序/副本/清理）；
2. Mock LLM 完整课堂：流式帧协议、7 轮 phase、随堂测验、分组研讨、学情档案；
3. 断线重连补发（不丢尾消息、补发失败可重试）；
4. 补发只发给当前连接（多连接不串台）；
5. 补发与实时推送串行化（不插队、不乱序）；
6. 发送异常不中止课堂；
7. 全部学生超时（order_alerts）时告警不吞轮次、路由不卡死；
8. 环节超时告警（overtime）可触发且课堂正常结束。
"""
import asyncio
import json
import os
import sys
from collections import Counter
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

# 测试隔离：不加载真实向量索引、不连 Redis、关闭流式打字延迟
os.environ["VECTOR_INDEX_PATH"] = str(BACKEND_DIR / "_no_index_for_tests")
os.environ["REDIS_URL"] = ""
os.environ["STREAM_INTERVAL_SECONDS"] = "0"

import llm_client  # noqa: E402

# 强制使用内置 MockLLM，避免测试消耗真实 API 额度（必须在 import classroom 之前设置）
llm_client.LLM_API_KEY = ""

import classroom  # noqa: E402
import config  # noqa: E402
from main import replay_missed  # noqa: E402
from store import SessionStore  # noqa: E402


async def _list_frames(session) -> list[dict]:
    return [json.loads(item) for item in await session.store.list_messages(session.session_id)]


def _logical_messages(frames: list[dict]) -> list[dict]:
    """按 id 聚合 message_start/delta/end 帧，还原逻辑消息（含开始帧元数据与完整内容）。"""
    by_id: dict[int, dict] = {}
    order: list[int] = []
    for frame in frames:
        frame_type = frame.get("type")
        if frame_type == "message_start":
            by_id[frame["id"]] = {"id": frame["id"], "content": "", "start": frame}
            order.append(frame["id"])
        elif frame_type == "message_delta":
            by_id.setdefault(frame["id"], {"id": frame["id"], "content": "", "start": {}})
            by_id[frame["id"]]["content"] += frame.get("delta", "")
        elif frame_type == "message_end":
            entry = by_id.setdefault(
                frame["id"], {"id": frame["id"], "content": "", "start": {}}
            )
            entry["end"] = frame
            entry["content"] = frame.get("content", "")
    return [by_id[mid] for mid in order]


async def test_store_buffer():
    """SessionStore 消息缓冲：追加顺序、返回副本、按会话清理"""
    store = SessionStore("")
    for i in range(1, 4):
        await store.append_message("sid-a", json.dumps({"id": i}))
    items = await store.list_messages("sid-a")
    assert [json.loads(x)["id"] for x in items] == [1, 2, 3]
    items.append("mutated")  # 返回副本，不能影响存储
    assert len(await store.list_messages("sid-a")) == 3
    await store.clear_session("sid-a")
    assert await store.list_messages("sid-a") == []
    print("[1/8] SessionStore 消息缓冲 OK")


async def test_full_flow():
    """Mock LLM 跑完整课堂：流式帧、随堂测验、分组研讨、学情档案"""
    session = classroom.create_session("高等数学 第一章 极限与连续")
    await session.run()
    frames = await _list_frames(session)

    student_count = len(config.STUDENT_PERSONALITIES)
    expected_messages = 7 + student_count * 4 + 1 + 1  # 老师7 + 学生4轮 + 助教 + 管理员

    types = Counter(frame.get("type") for frame in frames)
    assert types["message_start"] == expected_messages, types
    assert types["message_end"] == expected_messages, types
    assert types["message_delta"] > 0, "应有流式分片帧"
    assert types["profile"] == 1 and types["end"] == 1, types
    assert frames[-1]["type"] == "end", "最后一帧应为 end"
    assert frames[-2]["type"] == "profile", "end 之前应推送学情档案"

    # 每个逻辑消息：start/delta/end 共用一个 id，且 delta 拼接后等于完整 content
    by_id: dict[int, dict] = {}
    for frame in frames:
        if frame.get("type") == "message_start":
            by_id[frame["id"]] = {"start": frame, "deltas": [], "end": None}
        elif frame.get("type") == "message_delta":
            by_id[frame["id"]]["deltas"].append(frame.get("delta", ""))
        elif frame.get("type") == "message_end":
            by_id[frame["id"]]["end"] = frame
    assert len(by_id) == expected_messages
    for mid, parts in by_id.items():
        assert parts["end"] is not None, f"消息 {mid} 缺少 message_end"
        assert "".join(parts["deltas"]) == parts["end"].get("content", ""), f"消息 {mid} 分片内容不一致"
        assert parts["start"].get("speaker") == parts["end"].get("speaker")

    messages = _logical_messages(frames)
    teacher = [m["end"] for m in messages if m["end"]["speaker"] == "teacher"]
    assert [m["round"] for m in teacher] == list(range(1, 8)), "老师轮次应严格 1..7"
    assert [m["phase"] for m in teacher] == [
        "teaching", "answer", "answer", "answer", "discuss", "grading", "summary",
    ], "老师阶段应与实际环节一致"

    # 随堂测验：round2 老师消息带 quiz，全部学生作答且答对（Mock 固定选 B、答案索引 1）
    quiz_teacher = next(m for m in teacher if m["round"] == 2)
    assert quiz_teacher.get("quiz"), "round2 老师消息应包含随堂测验"
    quiz = quiz_teacher["quiz"]
    assert quiz.get("question") and len(quiz.get("options") or []) == 4
    student_msgs = [m["end"] for m in messages if m["end"]["speaker"].startswith("student_")]
    assert len(student_msgs) == student_count * 4
    answered = [m for m in student_msgs if m.get("answer")]
    assert len(answered) == student_count, f"应有 {student_count} 条学生作答，实际 {len(answered)}"
    assert all(m["answer"]["quiz_id"] == quiz["quiz_id"] for m in answered)
    assert all(m["answer"]["choice_index"] == quiz["answer_index"] for m in answered)

    # 分组研讨：round4 学生消息带 group，覆盖全部学生
    discuss = [m for m in student_msgs if m.get("group")]
    assert len(discuss) == student_count, f"研讨消息应全部带分组，实际 {len(discuss)}"
    group_ids = {m["group"]["group_id"] for m in discuss}
    assert len(group_ids) >= 2, f"应至少 2 个互补分组，实际 {group_ids}"

    # 学情档案
    profile = frames[-2]["profile"]
    assert profile["topic"] == session.topic
    assert profile["rounds"] == 7
    assert profile["messages"] == expected_messages
    assert len(profile["students"]) == student_count
    assert profile["quiz"]["total_answered"] == student_count
    assert profile["quiz"]["accuracy"] == 1.0, "Mock 作答应全部正确"
    assert profile["interaction"]["alerts"] == 0
    assert profile["groups"], "学情档案应包含小组统计"
    assert any(s["answers"] == 1 for s in profile["students"])
    saved_profile = await classroom.load_profile(session.session_id)
    assert saved_profile is not None and saved_profile["rounds"] == 7

    assert session.message_count == expected_messages
    assert session.state["round"] == 7 and session.state["phase"] == "end"
    assert not session.running

    # 重复 start 幂等
    seq_before = session.msg_seq
    await session.run()
    assert session.msg_seq == seq_before, "重复 run 不应产生新帧"
    print(f"[2/8] 完整课堂流程 OK（{expected_messages} 条消息流式帧、测验/分组/学情档案齐备）")


async def test_replay_last_message():
    """断线重连补发：last_msg_id 之后的消息（含最后一条）不能丢；补发失败时保留缓冲"""
    session = classroom.create_session("断线续传测试")
    # 模拟 3 条已完成消息（每条 start/delta/end 共用一个 id）
    for i in range(1, 4):
        for frame in (
            {"type": "message_start", "id": i, "speaker": "teacher"},
            {"type": "message_delta", "id": i, "delta": f"m{i}"},
            {"type": "message_end", "id": i, "speaker": "teacher", "content": f"m{i}"},
        ):
            await session.store.append_message(session.session_id, json.dumps(frame))

    sent: list[dict] = []

    class FakeWebSocket:
        async def send_json(self, payload):
            sent.append(payload)

    session.ws = FakeWebSocket()
    await replay_missed(session, session.ws, 1)
    assert [p["id"] for p in sent] == [2, 2, 2, 3, 3, 3], f"补发帧不完整: {[p['id'] for p in sent]}"
    assert [p["type"] for p in sent] == [
        "message_start", "message_delta", "message_end",
    ] * 2
    # 缓冲不裁剪：整堂课帧保留，供多客户端/刷新后按 last_msg_id 补历史
    assert len(await session.store.list_messages(session.session_id)) == 9

    # 补发途中断线：缓冲必须保留，等下次重连继续补发（独立会话避免叠加）
    failure_session = classroom.create_session("断线续传-补发失败")
    for i in range(1, 6):
        await failure_session.store.append_message(
            failure_session.session_id, json.dumps({"type": "message_end", "id": i})
        )

    class BrokenWebSocket:
        async def send_json(self, payload):
            raise RuntimeError("connection closed")

    failure_session.ws = BrokenWebSocket()
    await replay_missed(failure_session, failure_session.ws, 2)
    kept = await failure_session.store.list_messages(failure_session.session_id)
    assert len(kept) == 5, "补发失败时不能裁剪缓冲，否则消息会永久丢失"
    print("[3/8] 断线重连续传 OK（尾消息未丢失，补发失败可重试）")


async def test_replay_targets_given_ws():
    """replay 必须发给传入的「当前连接」，而不是 session.ws（双连接并发补发时不能错发）"""
    session = classroom.create_session("多连接补发测试")
    for i in range(1, 3):
        await session.store.append_message(
            session.session_id, json.dumps({"type": "message_end", "id": i})
        )

    sent_a, sent_b = [], []

    class FakeWebSocket:
        def __init__(self, sink):
            self._sink = sink

        async def send_json(self, payload):
            self._sink.append(payload["id"])

    session.ws = FakeWebSocket(sent_b)
    await replay_missed(session, FakeWebSocket(sent_a), 0)
    assert sent_a == [1, 2], f"A 连接应收到全部历史，实际 {sent_a}"
    assert sent_b == [], f"不能把补发消息错发到 session.ws，实际 {sent_b}"
    print("[4/8] 补发使用显式连接 OK（不会错发到其他客户端）")


async def test_replay_serialized_with_live_push():
    """重连补发期间产生的实时消息必须排在补发消息之后（不能插队导致乱序/漏补）"""
    session = classroom.create_session("乱序回归测试")
    # 与预置的 3 条历史帧对齐，保证新消息 id 从 4 开始
    session.msg_seq = 3
    for i in range(1, 4):
        await session.store.append_message(
            session.session_id, json.dumps({"type": "message_end", "id": i})
        )

    order: list[int] = []

    class SlowWebSocket:
        async def send_json(self, payload):
            order.append(payload["id"])
            await asyncio.sleep(0.02)  # 模拟网络延迟，给并发推送插队的机会

    session.ws = SlowWebSocket()

    async def push_during_replay():
        await asyncio.sleep(0.01)  # 等补发开始后再产生新消息
        await session.push_message({"type": "message", "speaker": "teacher", "content": "m4"})

    await asyncio.gather(replay_missed(session, session.ws, 0), push_during_replay())
    assert order[:3] == [1, 2, 3], f"补发顺序错误: {order[:3]}"
    assert order[3:] == [4, 4, 4], f"新消息应整体排在补发之后: {order[3:]}"
    assert order == sorted(order), f"帧 id 必须非递减: {order}"
    print("[5/8] 重连补发与实时推送串行化 OK（不插队、不乱序）")


async def test_send_failure_does_not_abort_class():
    """发送时连接断开的异常类型不可枚举（RuntimeError/WebSocketDisconnect/OSError），不能中断课堂"""
    session = classroom.create_session("发送失败测试")

    class BrokenWebSocket:
        async def send_json(self, payload):
            raise OSError("client disconnected")

    session.ws = BrokenWebSocket()
    # 不应抛出任何异常：帧先入缓冲，等重连补发；同时把失效连接标记为 None
    await session.push_message({"type": "message", "speaker": "teacher", "content": "m1"})
    assert session.ws is None, "发送失败后应把失效连接置空，避免后续消息继续撞错连接"
    types = Counter(f["type"] for f in await _list_frames(session))
    assert types["message_start"] == 1 and types["message_end"] == 1
    print("[6/8] 发送异常不中止课堂 OK（帧已缓冲，连接已置空）")


async def test_order_alerts_do_not_consume_rounds():
    """学生全部超时产生 order_alerts 时：告警由 manager 处理并清空，不能吞掉教学轮次或死循环"""
    import agents.student as student_module

    original_respond = student_module.StudentAgent.respond
    original_answer = getattr(student_module.StudentAgent, "answer_quiz", None)

    async def always_timeout(*args, **kwargs):
        raise asyncio.TimeoutError("模拟学生 Agent 超时")

    student_module.StudentAgent.respond = always_timeout
    if original_answer is not None:
        student_module.StudentAgent.answer_quiz = always_timeout
    try:
        session = classroom.create_session("告警路由测试")
        await session.run()
    finally:
        student_module.StudentAgent.respond = original_respond
        if original_answer is not None:
            student_module.StudentAgent.answer_quiz = original_answer

    frames = await _list_frames(session)
    alerts = [f for f in frames if f.get("type") == "alert"]
    messages = _logical_messages(frames)
    teacher = [m["end"] for m in messages if m["end"]["speaker"] == "teacher"]
    manager = [m["end"] for m in messages if m["end"]["speaker"] == "manager"]
    assert alerts, "学生全部超时应产生 order_alerts 告警"
    assert len(teacher) == 7 and [m["round"] for m in teacher] == list(range(1, 8)), (
        f"告警不应吞掉老师轮次，实际 {[m['round'] for m in teacher]}"
    )
    assert len(manager) >= 2, f"manager 应至少推送 2 条，实际 {len(manager)}"
    assert session.state["round"] == 7 and session.state["phase"] == "end"
    assert not session.running
    print(f"[7/8] order_alerts 路由 OK（{len(alerts)} 条告警，老师仍 7 轮，manager {len(manager)} 条）")


async def test_stage_timeout_alert():
    """环节超时（overtime）告警可触发，课堂仍能正常结束"""
    import graph.workflow as workflow_module

    original_stage = getattr(workflow_module, "STAGE_TIMEOUT_SECONDS", None)
    original_config = config.STAGE_TIMEOUT_SECONDS
    try:
        config.STAGE_TIMEOUT_SECONDS = -1.0
        if original_stage is not None:
            workflow_module.STAGE_TIMEOUT_SECONDS = -1.0
        session = classroom.create_session("超时告警测试")
        await session.run()
    finally:
        config.STAGE_TIMEOUT_SECONDS = original_config
        if original_stage is not None:
            workflow_module.STAGE_TIMEOUT_SECONDS = original_stage

    frames = await _list_frames(session)
    alerts = [f["alert"] for f in frames if f.get("type") == "alert"]
    assert any(a.get("type") == "overtime" for a in alerts), f"应有环节超时告警，实际 {alerts}"
    assert session.state["phase"] == "end" and session.state["round"] == 7
    print(f"[8/8] 环节超时告警 OK（{len([a for a in alerts if a.get('type') == 'overtime'])} 条 overtime，课堂正常结束）")


async def _run_all():
    await test_store_buffer()
    await test_full_flow()
    await test_replay_last_message()
    await test_replay_targets_given_ws()
    await test_replay_serialized_with_live_push()
    await test_send_failure_does_not_abort_class()
    await test_order_alerts_do_not_consume_rounds()
    await test_stage_timeout_alert()
    print("\nALL P0/P1 REGRESSION TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(_run_all())
