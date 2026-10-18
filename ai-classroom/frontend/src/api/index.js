/**
 * REST API 封装：创建课堂会话与查询状态。
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
