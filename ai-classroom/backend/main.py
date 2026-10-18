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


async def replay_missed(session: classroom.ClassroomSession, last_msg_id: int):
    """断线续传：补发 last_msg_id 之后缓冲的消息，成功后裁剪已补发部分。

    由于发送成功的消息也会入缓冲（见 classroom.send_or_buffer），这里按 last_msg_id 去重：
    客户端已收到的消息 id 不会重复推送；补发中途再次断线时保留缓冲，等下次重连继续。
    """
    buffered = await session.buffer.peek_all(session.buffer_key)
    try:
        for item in buffered:
            payload = json.loads(item)
            if payload.get("id", 0) > last_msg_id:
                await session.ws.send_json(payload)
    except Exception:
        # 补发过程中连接再次断开：保留缓冲，待下次重连按新的 last_msg_id 继续补发
        return
    # 裁剪已成功补发的快照（补发期间新追加的消息保留在缓冲中）
    await session.buffer.remove_first(session.buffer_key, len(buffered))


@app.websocket("/ws/{session_id}")
async def ws_endpoint(ws: WebSocket, session_id: str, last_msg_id: int = 0):
    """课堂 WebSocket 端点：心跳 ping/pong + 启动授课指令。"""
    session = classroom.sessions.get(session_id)
    if session is None:
        await ws.close(code=4004, reason="会话不存在")
        return

    await ws.accept()
    session.ws = ws

    # 断线续传：连接建立后按 last_msg_id 补发断线期间的消息（新连接 last_msg_id=0 时补发全部）
    await replay_missed(session, last_msg_id)

    try:
        while True:
            data = await ws.receive_json()
            msg_type = data.get("type")
            if msg_type == "ping":
                # 心跳保活：前端每 30s 发送一次
                await ws.send_json({"type": "pong"})
            elif msg_type == "start":
                # 启动课堂流程（后台异步执行，不阻塞当前接收循环）
                if not session.running:
                    asyncio.create_task(session.run())
    except WebSocketDisconnect:
        # 前端断开：保持会话与缓冲，等待重连续传
        pass
    finally:
        session.ws = None


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
