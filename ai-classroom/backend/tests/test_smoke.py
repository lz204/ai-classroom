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
    print("[1/3] MessageBuffer LRANGE 语义 OK")


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
    print(f"[2/3] 完整课堂流程 OK（{expected_messages} 条消息，{student_count} 名学生）")


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
    await replay_missed(session, 1)
    assert [p["id"] for p in sent] == [2, 3], f"补发消息不完整: {[p['id'] for p in sent]}"
    assert await session.buffer.lrange(session.buffer_key, 0, -1) == []

    # 补发途中断线：缓冲必须保留，等下次重连继续补发
    session.msg_seq = 5
    for i in range(1, 6):
        await session.buffer.rpush(
            session.buffer_key,
            json.dumps({"type": "message", "id": i, "content": f"m{i}"}, ensure_ascii=False),
        )

    class BrokenWebSocket:
        async def send_json(self, payload):
            raise RuntimeError("connection closed")

    session.ws = BrokenWebSocket()
    await replay_missed(session, 2)
    kept = await session.buffer.lrange(session.buffer_key, 0, -1)
    assert len(kept) == 5, "补发失败时不能裁剪缓冲，否则消息会永久丢失"
    print("[3/3] 断线重连续传 OK（尾消息未丢失，补发失败可重试）")


async def _run_all():
    await test_buffer_lrange()
    await test_full_flow()
    await test_replay_last_message()
    print("\nALL P0 REGRESSION TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(_run_all())
