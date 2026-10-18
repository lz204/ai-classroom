"""FastAPI 入口：REST 路由 + WebSocket 课堂实时通信（心跳保活/断线续传）。

启动（backend/ 目录下）：
    uvicorn main:app --reload --port 8000
"""
import asyncio
import json

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

import classroom
from api.routes import router

app = FastAPI(title="AI Classroom 多智能体互动教学课堂")

# 允许前端跨域（Vite 开发端口 5173 等）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)


@app.get("/")
async def root():
    """根路径：返回服务入口说明（避免直接访问根路径时显示 404）"""
    return {
        "name": "AI Classroom 多智能体互动教学课堂",
        "api_docs": "/docs",
        "health": "/api/health",
        "start_classroom": "POST /api/classroom/start",
        "classroom_status": "/api/classroom/{session_id}",
        "websocket": "/ws/{session_id}",
        "frontend": "http://localhost:5173",
    }


async def replay_missed(session: classroom.ClassroomSession, ws: WebSocket, last_msg_id: int):
    """断线续传：向当前连接补发 last_msg_id 之后缓冲的消息（不裁剪缓冲）。

    - 必须显式传入「当前连接」的 ws：若用 session.ws，双连接并发补发时消息会被发到另一个连接，
      造成两端都缺消息；缓冲也保留整堂课内容，便于多客户端/刷新后按 last_msg_id 补齐历史。
    - 整个过程持有 session._send_lock，与实时推送串行，保证客户端收到的消息严格按 id 递增；
      补发中途连接再次断开时直接返回，消息仍在缓冲中，等下次重连按新的 last_msg_id 继续补发。
    """
    async with session._send_lock:
        buffered = await session.buffer.peek_all(session.buffer_key)
        try:
            for item in buffered:
                payload = json.loads(item)
                if payload.get("id", 0) > last_msg_id:
                    await asyncio.wait_for(ws.send_json(payload), timeout=classroom.SEND_TIMEOUT)
        except Exception:
            # 补发过程中连接断开/超时：保留缓冲，等下次重连继续；若会话仍指向该连接则标记失效
            if session.ws is ws:
                session.ws = None


@app.websocket("/ws/{session_id}")
async def ws_endpoint(ws: WebSocket, session_id: str, last_msg_id: int = 0):
    """课堂 WebSocket 端点：心跳 ping/pong + 启动授课指令。"""
    session = classroom.sessions.get(session_id)
    if session is None:
        # 先 accept 再 close，浏览器才能拿到 4004 关闭码（accept 前 close 会退化成 HTTP 403）
        await ws.accept()
        await ws.close(code=4004, reason="会话不存在")
        return

    await ws.accept()
    session.ws = ws

    # 断线续传：连接建立后按 last_msg_id 补发断线期间的消息（新连接 last_msg_id=0 时补发全部）
    await replay_missed(session, ws, last_msg_id)

    try:
        while True:
            data = await ws.receive_json()
            msg_type = data.get("type")
            if msg_type == "ping":
                # 心跳保活：前端每 30s 发送一次
                await ws.send_json({"type": "pong"})
            elif msg_type == "start":
                # 启动课堂流程（后台异步执行，不阻塞当前接收循环）；只允许启动一次
                if not session.running and not session.started:
                    asyncio.create_task(session.run())
    except WebSocketDisconnect:
        # 前端断开：保持会话与缓冲，等待重连续传
        pass
    finally:
        # 只有当前连接仍然是会话的活动连接时才清空，避免误清掉后建立的新连接
        if session.ws is ws:
            session.ws = None


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
