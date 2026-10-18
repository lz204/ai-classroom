/**
 * WebSocket 通信封装：心跳保活 + 断线指数退避重连 + last_msg_id 断线续传。
 * 与后端 /ws/{session_id} 端点配合，保证课堂消息不丢失、不重复。
 *
 * P1 流式协议（与 backend/classroom.py::push_message 对齐）：
 * - 一条逻辑消息拆成 message_start → message_delta* → message_end 三类帧，共用同一自增 id；
 * - lastMsgId 只在「完整/终结」帧上推进：message_end / alert / end / error / 旧协议 message。
 *   message_start / message_delta / profile 不推进：断线时若半条流式消息只收到部分 delta，
 *   lastMsgId 仍停在上一条已完成消息，重连后服务端会把该消息的 start/delta/end 完整重发，
 *   前端按 id 去重并重置气泡后重新累积，避免缺字（见 composables/useMessageStream.js）。
 */

// 重连退避序列（秒），最多重试 MAX_RECONNECT_ATTEMPTS 次
const RECONNECT_DELAYS = [1, 2, 4, 8]
// 自动重连次数上限：超过后停止重试并提示用户（会话不存在/服务长时间不可用）
const MAX_RECONNECT_ATTEMPTS = 5
// 服务端「会话不存在」关闭码（main.py 中 accept 后 close(4004)）
const CLOSE_SESSION_NOT_FOUND = 4004

// 只有这些「完整/终结」帧才推进 lastMsgId，保证半条流式消息重连后能从 start 帧完整补发
const ID_ADVANCING_TYPES = new Set(['message', 'message_end', 'alert', 'end', 'error'])

export default class ClassroomSocket {
  /**
   * @param {string} sessionId 课堂会话 ID
   * @param {object} handlers 事件回调：
   *   { onMessageStart, onMessageDelta, onMessageEnd, onMessage,
   *     onProfile, onAlert, onEnd, onError, onStatus }
   */
  constructor(sessionId, handlers = {}) {
    this.sessionId = sessionId
    this.handlers = handlers
    this.ws = null
    this.lastMsgId = 0 // 已收到的最后一条「完整帧」id（断线续传用）
    this.reconnectIndex = 0
    this.heartbeatTimer = null
    this.manualClosed = false
    this.ended = false // 课堂已结束：不再自动重连
  }

  /** 建立连接（携带 last_msg_id 以便补发断线期间的消息） */
  connect() {
    this.manualClosed = false
    const url = `/ws/${this.sessionId}?last_msg_id=${this.lastMsgId}`
    const proto = window.location.protocol === 'https:' ? 'wss' : 'ws'
    this.ws = new WebSocket(`${proto}://${window.location.host}${url}`)

    this.ws.onopen = () => {
      this.reconnectIndex = 0
      this._startHeartbeat()
      this.handlers.onStatus?.('connected')
    }

    this.ws.onmessage = (event) => {
      let data
      try {
        data = JSON.parse(event.data)
      } catch (err) {
        // 单帧解析失败不应中断连接（避免未捕获异常影响后续消息）
        console.warn('[ClassroomSocket] 收到无法解析的帧，已忽略:', err)
        return
      }
      this._dispatch(data)
    }

    this.ws.onclose = (event) => {
      this._stopHeartbeat()
      this.handlers.onStatus?.('disconnected')
      if (this.manualClosed || this.ended) return
      // 会话不存在（4004）：重连也不会有结果，提示用户重新发起授课
      if (event.code === CLOSE_SESSION_NOT_FOUND) {
        this.handlers.onError?.({ content: '课堂会话不存在或已失效，请重新发起授课' })
        return
      }
      // 重连次数达到上限后停止，避免对不可用服务无限重试
      if (this.reconnectIndex >= MAX_RECONNECT_ATTEMPTS) {
        this.handlers.onError?.({ content: '连接已中断且自动重连失败，请刷新页面重试' })
        return
      }
      // 指数退避重连（上限 8s）
      const delay = RECONNECT_DELAYS[Math.min(this.reconnectIndex, RECONNECT_DELAYS.length - 1)]
      this.reconnectIndex += 1
      setTimeout(() => this.connect(), delay * 1000)
    }

    this.ws.onerror = () => {
      // 错误后由 onclose 统一触发重连
      this.ws?.close()
    }
  }

  /** 帧路由：按 type 分发到对应回调，并维护 lastMsgId */
  _dispatch(data) {
    if (!data || typeof data !== 'object') return
    const id = Number(data.id)
    if (ID_ADVANCING_TYPES.has(data.type) && Number.isFinite(id)) {
      // 同一逻辑消息的多个帧共用同一 id，取 max 不会重复递增
      this.lastMsgId = Math.max(this.lastMsgId, id)
    }

    switch (data.type) {
      case 'message_start':
        this.handlers.onMessageStart?.(data)
        break
      case 'message_delta':
        this.handlers.onMessageDelta?.(data)
        break
      case 'message_end':
        this.handlers.onMessageEnd?.(data)
        break
      case 'message':
        // 兼容旧协议 / 历史回放：一次性完整消息
        this.handlers.onMessage?.(data)
        break
      case 'profile':
        this.handlers.onProfile?.(data)
        break
      case 'alert':
        this.handlers.onAlert?.(data)
        break
      case 'end':
        this.ended = true
        this.handlers.onEnd?.(data)
        break
      case 'error':
        this.handlers.onError?.(data)
        break
      case 'pong':
        // 心跳响应：无需额外处理
        break
      default:
        // 未知帧类型：静默忽略，保证向前兼容
        break
    }
  }

  /** 发送"开始授课"指令 */
  sendStart() {
    this._send({ type: 'start' })
  }

  /** 手动关闭连接（不再自动重连） */
  close() {
    this.manualClosed = true
    this._stopHeartbeat()
    this.ws?.close()
  }

  _send(payload) {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(payload))
    } else {
      console.warn('[ClassroomSocket] 连接未就绪，消息未发送:', payload)
    }
  }

  _startHeartbeat() {
    // 每 30s 发送心跳，避免 Nginx 因空闲切断连接
    this.heartbeatTimer = setInterval(() => {
      this._send({ type: 'ping' })
    }, 30000)
  }

  _stopHeartbeat() {
    if (this.heartbeatTimer) {
      clearInterval(this.heartbeatTimer)
      this.heartbeatTimer = null
    }
  }
}
