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
    """查询课堂会话状态（本进程没有会话时从持久化存储读取状态快照）"""
    session = classroom.sessions.get(session_id)
    if session is None:
        meta = await classroom.load_session_meta(session_id)
        if meta is None:
            raise HTTPException(status_code=404, detail="会话不存在")
        meta.setdefault("session_id", session_id)
        return meta
    return {
        "session_id": session.session_id,
        "topic": session.topic,
        "running": session.running,
        "msg_count": session.msg_seq,
        "round": session.state.get("round"),
        "phase": session.state.get("phase"),
        "message_count": session.message_count,
        "profile_ready": session.profile_data is not None,
    }


@router.get("/classroom/{session_id}/profile")
async def classroom_profile(session_id: str):
    """查询课堂学情档案（课堂结束后生成；进程内会话或持久化存储中查询）"""
    session = classroom.sessions.get(session_id)
    if session is not None and session.profile_data is not None:
        return session.profile_data
    profile = await classroom.load_profile(session_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="学情档案尚未生成或会话不存在")
    return profile
