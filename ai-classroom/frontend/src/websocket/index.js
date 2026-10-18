/**
 * WebSocket 通信封装：心跳保活 + 断线指数退避重连 + last_msg_id 断线续传。
 * 与后端 /ws/{session_id} 端点配合，保证课堂消息不丢失、不重复。
 */

// 重连退避序列（秒）
const RECONNECT_DELAYS = [1, 2, 4, 8]

export default class ClassroomSocket {
  /**
   * @param {string} sessionId 课堂会话 ID
   * @param {object} handlers 事件回调：{ onMessage, onAlert, onEnd, onError, onStatus }
   */
  constructor(sessionId, handlers = {}) {
    this.sessionId = sessionId
    this.handlers = handlers
    this.ws = null
    this.lastMsgId = 0 // 已收到的最新消息 id（断线续传用）
    this.reconnectIndex = 0
    this.heartbeatTimer = null
    this.manualClosed = false
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
      const data = JSON.parse(event.data)
      if (data.id) {
        // 记录最新消息 id，断线重连时据此续传
        this.lastMsgId = Math.max(this.lastMsgId, data.id)
      }
      if (data.type === 'message') {
        this.handlers.onMessage?.(data)
      } else if (data.type === 'alert') {
        this.handlers.onAlert?.(data)
      } else if (data.type === 'end') {
        this.handlers.onEnd?.(data)
      } else if (data.type === 'error') {
        this.handlers.onError?.(data)
      } else if (data.type === 'pong') {
        // 心跳响应：无需额外处理
      }
    }

    this.ws.onclose = () => {
      this._stopHeartbeat()
      this.handlers.onStatus?.('disconnected')
      if (this.manualClosed) return
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
