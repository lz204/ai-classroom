import { ref } from 'vue'

/** 气泡时间戳（hh:mm:ss） */
function currentTime() {
  return new Date().toLocaleTimeString('zh-CN', { hour12: false })
}

/**
 * 课堂消息流聚合器：把流式帧按「逻辑消息 id」聚合成气泡列表。
 *
 * 与后端 classroom.py::push_message 的协议对齐：
 * - 一条逻辑消息拆成 message_start → message_delta* → message_end 三类帧，三者共用同一自增 id；
 * - message_delta 只追加到同 id 的占位气泡，绝不作为新消息追加；
 * - 断线重连时服务端会按 last_msg_id 补发（lastMsgId 不在 start/delta 上推进，所以半条消息会从
 *   start 帧完整重发）：同 id 的 start 若已有气泡，先重置 content 再重新累积 delta，保证不重影、不缺字；
 * - 兼容旧协议 message 帧（一次性完整消息，断线补发/历史回放会使用）：同 id 覆盖字段而非重复追加。
 */
export function useMessageStream() {
  const messages = ref([])
  // 逻辑消息 id -> messages 下标；用于 start/delta/end 聚合与重连补发去重
  const messageIndexById = new Map()
  // 无 id 的兜底消息自增 key（正常协议下不会用到）
  let localSeq = 0

  function createMessage(data = {}, streaming = false) {
    const hasId = data.id != null
    return {
      key: hasId ? `message-${data.id}` : `message-local-${(localSeq += 1)}`,
      id: hasId ? data.id : null,
      speaker: data.speaker || '',
      // 后端在帧里给出真实姓名（学生：同学N），课前即可正确显示；缺省时由页面兜底
      speakerName: data.speaker_name || '',
      content: data.content || '',
      citation: Array.isArray(data.citation) ? data.citation : [],
      quiz: data.quiz || null,
      answer: data.answer || null,
      group: data.group || null,
      phase: data.phase || '',
      round: data.round ?? 0,
      time: currentTime(),
      streaming,
    }
  }

  function findById(id) {
    if (id == null) return null
    const index = messageIndexById.get(id)
    if (index == null) return null
    return messages.value[index] || null
  }

  function append(data, streaming) {
    const message = createMessage(data, streaming)
    messages.value.push(message)
    if (message.id != null) messageIndexById.set(message.id, messages.value.length - 1)
    return message
  }

  /** message_start：创建占位气泡；同 id 已存在（重连补发）时重置内容后重新累积 delta */
  function applyStart(data) {
    if (!data || data.id == null) return null
    const existing = findById(data.id)
    if (!existing) return append(data, true)

    existing.speaker = data.speaker || existing.speaker
    if (data.speaker_name) existing.speakerName = data.speaker_name
    existing.content = ''
    if (Array.isArray(data.citation)) existing.citation = data.citation
    if (data.quiz) existing.quiz = data.quiz
    if (data.answer) existing.answer = data.answer
    if (data.group) existing.group = data.group
    if (data.phase) existing.phase = data.phase
    if (data.round != null) existing.round = data.round
    existing.time = existing.time || currentTime()
    existing.streaming = true
    return existing
  }

  /** message_delta：把分片追加到同 id 气泡；start 缺失时兜底建占位，避免丢字 */
  function applyDelta(data) {
    if (!data || data.id == null) return null
    let message = findById(data.id)
    if (!message) message = append({ id: data.id }, true)
    if (typeof data.delta === 'string') message.content += data.delta
    message.streaming = true
    return message
  }

  /** message_end：用完整 content/citation/answer/quiz/group 收尾；只收到 end 时也能完整渲染 */
  function applyEnd(data) {
    if (!data || data.id == null) return null
    const existing = findById(data.id)
    if (!existing) return append(data, false)

    if (typeof data.content === 'string') existing.content = data.content
    if (Array.isArray(data.citation)) existing.citation = data.citation
    if (data.quiz) existing.quiz = data.quiz
    if (data.answer) existing.answer = data.answer
    if (data.group) existing.group = data.group
    if (data.speaker) existing.speaker = data.speaker
    if (data.speaker_name) existing.speakerName = data.speaker_name
    if (data.phase) existing.phase = data.phase
    if (data.round != null) existing.round = data.round
    existing.time = existing.time || currentTime()
    existing.streaming = false
    return existing
  }

  /** 旧协议 message：一次性完整消息；同 id 已存在时覆盖字段，避免补发/回放产生重复气泡 */
  function applyLegacy(data) {
    if (!data) return null
    const existing = findById(data.id)
    if (existing) {
      existing.speaker = data.speaker || existing.speaker
      if (data.speaker_name) existing.speakerName = data.speaker_name
      existing.content = data.content || ''
      if (Array.isArray(data.citation)) existing.citation = data.citation
      existing.quiz = data.quiz || null
      existing.answer = data.answer || null
      existing.group = data.group || null
      if (data.phase) existing.phase = data.phase
      if (data.round != null) existing.round = data.round
      existing.streaming = false
      return existing
    }
    return append(data, false)
  }

  /** 新课堂：清空消息与 id 索引 */
  function reset() {
    messages.value = []
    messageIndexById.clear()
    localSeq = 0
  }

  return { messages, applyStart, applyDelta, applyEnd, applyLegacy, reset }
}
