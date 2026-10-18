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
    topic = req.topic.strip()
    if not topic:
        raise HTTPException(status_code=400, detail="授课主题不能为空")
    if len(topic) > 200:
        # 输入上限：避免超长主题拼进 Prompt 造成 token 成本放大与注入面扩大
        raise HTTPException(status_code=400, detail="授课主题过长（最多 200 字符）")
    session = classroom.create_session(topic)
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


# ============ Tool-Use Agent 演示接口（Function Calling + MCP） ============
class AgentAskRequest(BaseModel):
    """工具 Agent 请求体"""

    question: str
    context: str = ""


# ToolAgent 单例：首次调用时初始化（会按需加载 RAG 检索器并连接 MCP Server）
_tool_agent = None


def _get_tool_agent():
    global _tool_agent
    if _tool_agent is None:
        from tool_agent import ToolAgent

        _tool_agent = ToolAgent()
    return _tool_agent


@router.get("/agent/tools")
async def agent_tools():
    """列出 Tool-Use Agent 可用工具（本地工具 + 通过 MCP 动态注册的工具）"""
    try:
        agent = _get_tool_agent()
        # 首次查看工具时先懒连接 MCP Server，否则只能看到本地工具；连接失败自动降级不抛错
        try:
            await agent.ensure_mcp(True)
        except Exception:
            pass
        return {
            "tools": agent.list_tools(),
            "mcp_connected": bool(getattr(agent, "mcp_connected", False)),
        }
    except Exception as exc:  # 初始化失败（如检索器/模型不可用）返回 503，不影响课堂主流程
        raise HTTPException(status_code=503, detail=f"ToolAgent 初始化失败：{exc!r}") from exc


async def close_agent():
    """应用关闭时释放 ToolAgent 的 MCP 子进程等资源（供 FastAPI lifespan 调用）。"""
    global _tool_agent
    agent, _tool_agent = _tool_agent, None
    if agent is not None:
        try:
            await agent.aclose()
        except Exception:
            pass


@router.post("/agent/ask")
async def agent_ask(req: AgentAskRequest):
    """让 Tool-Use Agent 执行一次「推理 → 工具调用 → 观察」循环并返回轨迹。

    返回：{answer, trace, steps, degraded, mcp_connected, tools_used}
    """
    question = req.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="问题不能为空")
    try:
        agent = _get_tool_agent()
        return await agent.ask(question, context=req.context)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"ToolAgent 执行失败：{exc!r}") from exc
