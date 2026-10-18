"""REST API 路由：健康检查、课堂会话创建与状态查询。"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import classroom

router = APIRouter(prefix="/api")


class StartRequest(BaseModel):
    """发起授课请求体"""

    topic: str


@router.get("/health")
async def health():
    """健康检查"""
    return {"status": "ok"}


@router.post("/classroom/start")
async def start_classroom(req: StartRequest):
    """创建课堂会话，返回 session_id（前端随后通过 WebSocket 连接并启动授课）"""
    if not req.topic.strip():
        raise HTTPException(status_code=400, detail="授课主题不能为空")
    session = classroom.create_session(req.topic.strip())
    return {"session_id": session.session_id, "topic": session.topic}


@router.get("/classroom/{session_id}")
async def classroom_status(session_id: str):
    """查询课堂会话状态"""
    session = classroom.sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    return {
        "session_id": session.session_id,
        "topic": session.topic,
        "running": session.running,
        "msg_count": session.msg_seq,
        "round": session.state.get("round"),
        "phase": session.state.get("phase"),
        "message_count": session.message_count,
    }
