/**
 * REST API 封装：创建课堂会话、查询状态，以及 Tool-Use Agent（Function Calling + MCP）演示接口。
 */

/** 创建课堂会话，返回 { session_id, topic } */
export async function startClassroom(topic) {
  const resp = await fetch('/api/classroom/start', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ topic }),
  })
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}))
    throw new Error(err.detail || '创建会话失败')
  }
  return resp.json()
}

/** 查询课堂会话状态 */
export async function fetchClassroomStatus(sessionId) {
  const resp = await fetch(`/api/classroom/${sessionId}`)
  if (!resp.ok) throw new Error('查询会话状态失败')
  return resp.json()
}

/**
 * Tool-Use Agent：执行一次「推理 → 工具调用 → 观察」演示。
 * @param {string} question 用户问题
 * @param {string} [context] 可选补充上下文（拼接进 Agent 的 user 消息）
 * @returns {Promise<{answer:string, trace:Array, steps:number, degraded:boolean, mcp_connected:boolean, tools_used:string[]}>}
 * 错误：400 空问题 / 503 Agent 初始化或执行失败，detail 为后端错误文案
 */
export async function askAgent(question, context = '') {
  const resp = await fetch('/api/agent/ask', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ question, context }),
  })
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}))
    throw new Error(err.detail || `Agent 运行失败（HTTP ${resp.status}）`)
  }
  return resp.json()
}

/**
 * Tool-Use Agent：查询可用工具列表（本地工具 + 通过 MCP 动态注册的 mcp__ 前缀工具）。
 * @returns {Promise<{tools:Array<{type:string, function:{name:string, description:string, parameters:object}}>, mcp_connected:boolean}>}
 * 错误：503 Agent 初始化失败，detail 为后端错误文案
 */
export async function fetchAgentTools() {
  const resp = await fetch('/api/agent/tools')
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}))
    throw new Error(err.detail || `获取可用工具失败（HTTP ${resp.status}）`)
  }
  return resp.json()
}
