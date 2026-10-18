<template>
  <div class="classroom-page">
    <!-- 顶栏：标题与连接状态 -->
    <header class="classroom-header">
      <h1 class="title">AI Classroom 多智能体互动教学课堂</h1>
      <div class="header-tags">
        <el-tag v-if="ended" type="info" size="large" effect="plain">课堂已结束</el-tag>
        <el-tag :type="connTagType" size="large">{{ connText }}</el-tag>
      </div>
    </header>

    <div class="classroom-body">
      <!-- 左侧：控制面板 + 答题面板 -->
      <aside class="control-panel">
        <el-card shadow="never">
          <template #header>课堂控制</template>
          <el-form @submit.prevent="handleStart">
            <el-form-item label="授课主题">
              <el-input
                v-model="topic"
                placeholder="例如：高等数学 第一章 极限与连续"
                clearable
              />
            </el-form-item>
            <el-form-item>
              <el-button type="primary" :loading="starting" @click="handleStart">
                开始授课
              </el-button>
            </el-form-item>
          </el-form>

          <el-divider />

          <div class="status-block">
            <div class="status-row">
              <span class="status-label">会话ID</span>
              <span class="status-value">{{ sessionId || '未创建' }}</span>
            </div>
            <div class="status-row">
              <span class="status-label">当前阶段</span>
              <span class="status-value">{{ phaseText }}</span>
            </div>
            <div class="status-row">
              <span class="status-label">轮次</span>
              <span class="status-value">{{ roundInfo }}</span>
            </div>
            <div class="status-row">
              <span class="status-label">消息数</span>
              <span class="status-value">{{ messages.length }}</span>
            </div>
            <div class="status-row">
              <span class="status-label">告警数</span>
              <span class="status-value">{{ alerts.length }}</span>
            </div>
          </div>

          <el-divider />

          <!-- 随堂测验 / 学生作答面板 -->
          <QuizPanel :quiz="currentQuiz" :answers="quizAnswers" :name-map="studentNameMap" />
        </el-card>
      </aside>

      <!-- 右侧：实时消息流 -->
      <main class="chat-panel">
        <el-alert
          v-if="latestAlertText"
          :title="latestAlertText"
          :type="latestAlertType"
          :closable="false"
          show-icon
          class="alert-bar"
        />
        <div ref="chatListRef" class="chat-list" @scroll="handleChatScroll">
          <el-empty v-if="!messages.length" description="发起授课后，课堂消息将实时展示在这里" />
          <MessageBubble
            v-for="message in messages"
            :key="message.key"
            :speaker="message.speaker"
            :speaker-label="messageSpeakerLabel(message)"
            :content="message.content"
            :citation="message.citation"
            :time="message.time"
            :phase="message.phase"
            :round="message.round"
            :group="message.group"
            :answer="message.answer"
            :quiz="message.quiz"
            :streaming="message.streaming"
          />
        </div>
      </main>
    </div>

    <!-- 学情档案：收到 profile 帧后展示（后端在 end 帧之前下发） -->
    <ProfileCard v-if="profile" :profile="profile" class="profile-panel" />

    <!-- Agent 工具演示（按需点击，不参与课堂自动流程，避免额外 LLM 调用） -->
    <ToolAgentPanel ref="toolAgentRef" />
  </div>
</template>

<script setup>
import { computed, nextTick, onBeforeUnmount, ref } from 'vue'
import { ElMessage } from 'element-plus'
import MessageBubble from '../components/MessageBubble.vue'
import QuizPanel from '../components/QuizPanel.vue'
import ProfileCard from '../components/ProfileCard.vue'
import ToolAgentPanel from '../components/ToolAgentPanel.vue'
import ClassroomSocket from '../websocket/index.js'
import { startClassroom } from '../api/index.js'
import { fallbackStudentName, formatDuration, phaseLabel } from '../utils/format.js'
import { useMessageStream } from '../composables/useMessageStream.js'

const topic = ref('')
const sessionId = ref('')
const socket = ref(null)
const starting = ref(false)
const alerts = ref([])
const profile = ref(null)
const connState = ref('idle') // idle | connecting | connected | disconnected
// 最近一次消息/控制帧上报的阶段与轮次（学生消息缺少该字段时不会回退）
const currentPhase = ref('')
const currentRound = ref(0)
const ended = ref(false)
const chatListRef = ref(null)
// Agent 工具演示面板：新课堂开始时通过 reset() 清空上一次的演示结果
const toolAgentRef = ref(null)
// 是否自动跟随到底部：用户向上回看历史时暂停自动滚动，滚回底部后恢复
const autoFollow = ref(true)

const { messages, applyStart, applyDelta, applyEnd, applyLegacy, reset: resetMessages } = useMessageStream()

// 本会话是否已发送过「开始授课」指令（重连时不再重复发送）
let startSent = false
// 流式 delta 高频到达时的滚动节流句柄
let scrollRaf = null

const connText = computed(() => {
  return { idle: '未连接', connecting: '连接中', connected: '已连接', disconnected: '已断开' }[connState.value]
})
const connTagType = computed(() => {
  return { idle: 'info', connecting: 'warning', connected: 'success', disconnected: 'danger' }[connState.value]
})
const phaseText = computed(() => phaseLabel(currentPhase.value) || '待开始')
const roundInfo = computed(() => (currentRound.value ? `第 ${currentRound.value} 轮` : '-'))

// ---------- 告警：按类型区分文案（timeout 学生走神 / overtime 环节超时 / class_timeout 整堂课超时） ----------
const latestAlert = computed(() => alerts.value[alerts.value.length - 1] || null)
const latestAlertText = computed(() => (latestAlert.value ? alertText(latestAlert.value) : ''))
const latestAlertType = computed(() => (latestAlert.value?.type === 'class_timeout' ? 'error' : 'warning'))

function durationText(value) {
  const seconds = Number(value)
  return Number.isFinite(seconds) ? formatDuration(seconds) : ''
}

function alertText(alert) {
  if (!alert) return ''
  const stage = alert.stage ? phaseLabel(alert.stage) : ''
  const elapsed = durationText(alert.elapsed)
  const limit = durationText(alert.limit)

  if (alert.type === 'timeout') {
    return `学生走神：${alert.student || '一名学生'}未能在规定时间内响应，管理员已介入提醒`
  }
  if (alert.type === 'overtime') {
    const parts = []
    if (stage) parts.push(`${stage}环节`)
    if (elapsed) parts.push(`已耗时 ${elapsed}`)
    if (limit) parts.push(`上限 ${limit}`)
    return `环节超时：${parts.join('，') || '当前教学环节超过时限'}，管理员已介入收束`
  }
  if (alert.type === 'class_timeout') {
    const parts = []
    if (elapsed) parts.push(`已耗时 ${elapsed}`)
    if (limit) parts.push(`上限 ${limit}`)
    return `整堂课超时：${parts.join('，') || '超过总时长限制'}，管理员已介入收束`
  }
  // 兼容旧格式：后端只带 student 字段的秩序提醒
  if (alert.student) return `秩序提醒：${alert.student}`
  return '课堂秩序提醒'
}

// ---------- 学生姓名映射：quiz 作答按 student_id 展示姓名 ----------
const studentNameMap = computed(() => {
  const map = {}
  for (const student of profile.value?.students || []) {
    if (student?.id) map[student.id] = student.name || fallbackStudentName(student.id)
  }
  return map
})

/** 气泡里的学生显示名：优先学情档案真实姓名，其次「同学N」兜底；老师/助教/管理员由气泡内置映射处理 */
function messageSpeakerLabel(message) {
  const speaker = message.speaker || ''
  if (!speaker || !speaker.startsWith('student_')) return ''
  // 优先用后端帧携带的真实姓名（speaker_name），其次学情档案，最后「同学N」兜底
  return message.speakerName || studentNameMap.value[speaker] || fallbackStudentName(speaker)
}

// ---------- 答题面板数据：当前测验 + 各学生作答 ----------
const currentQuiz = computed(() => {
  const list = messages.value
  // 取最后一条带 quiz 的消息（老师随堂测验消息）
  for (let index = list.length - 1; index >= 0; index -= 1) {
    const quiz = list[index].quiz
    if (quiz && (quiz.question || (Array.isArray(quiz.options) && quiz.options.length))) return quiz
  }
  return null
})

const quizAnswers = computed(() => {
  const quiz = currentQuiz.value
  if (!quiz) return []
  const answers = []
  for (const message of messages.value) {
    if (!message.answer) continue
    const answer = { ...message.answer }
    if (!answer.student_id) answer.student_id = message.speaker
    // 双方都有 quiz_id 时只统计当前测验的作答；字段缺失时兼容展示
    if (
      quiz.quiz_id != null &&
      answer.quiz_id != null &&
      String(quiz.quiz_id) !== String(answer.quiz_id)
    ) {
      continue
    }
    answers.push(answer)
  }
  return answers
})

// ---------- 帧处理 ----------
function syncPhaseAndRound(data) {
  if (data?.phase) currentPhase.value = data.phase
  if (data?.round != null) currentRound.value = data.round
}

function handleMessageStart(data) {
  applyStart(data)
  syncPhaseAndRound(data)
  scheduleScroll()
}

function handleMessageDelta(data) {
  // 只追加内容，不作为新消息（同一逻辑消息的 start/delta/end 共用一个 id）
  applyDelta(data)
  scheduleScroll()
}

function handleMessageEnd(data) {
  applyEnd(data)
  syncPhaseAndRound(data)
  scheduleScroll()
}

function handleLegacyMessage(data) {
  // 兼容旧协议 / 历史回放：一次性完整消息
  applyLegacy(data)
  syncPhaseAndRound(data)
  scheduleScroll()
}

function handleProfile(data) {
  if (!data?.profile) return
  syncPhaseAndRound(data)
  const firstProfile = !profile.value
  profile.value = data.profile
  if (firstProfile) ElMessage.success('课堂已结束，学情档案已生成')
}

function handleAlert(data) {
  const alert = data?.alert || {}
  alerts.value.push(alert)
  const text = alertText(alert)
  if (alert.type === 'class_timeout') ElMessage.error(text)
  else ElMessage.warning(text)
}

function handleEnd() {
  ended.value = true
  currentPhase.value = 'end'
  if (!profile.value) ElMessage.success('课堂流程已结束')
}

function handleError(data) {
  ElMessage.error(data?.content || '课堂运行异常')
}

// ---------- 滚动跟随 ----------
function scrollToBottom() {
  nextTick(() => {
    const list = chatListRef.value
    if (!list || !autoFollow.value) return
    list.scrollTop = list.scrollHeight
  })
}

function scheduleScroll() {
  // delta 帧可能几十毫秒一条：用 requestAnimationFrame 合并滚动，避免每帧都触发 layout
  if (scrollRaf != null) return
  scrollRaf = requestAnimationFrame(() => {
    scrollRaf = null
    scrollToBottom()
  })
}

function handleChatScroll() {
  const list = chatListRef.value
  if (!list) return
  const distanceToBottom = list.scrollHeight - list.scrollTop - list.clientHeight
  autoFollow.value = distanceToBottom < 120
}

// ---------- 连接管理 ----------
function connectSocket(sid) {
  socket.value?.close()
  startSent = false

  let instance = null
  // 旧连接（重开新课堂/重连前的实例）的迟到回调不得污染当前课堂
  const isCurrent = () => socket.value === instance
  const guarded = (handler) => (data) => {
    if (!isCurrent()) return
    handler(data)
  }

  instance = new ClassroomSocket(sid, {
    onMessageStart: guarded(handleMessageStart),
    onMessageDelta: guarded(handleMessageDelta),
    onMessageEnd: guarded(handleMessageEnd),
    onMessage: guarded(handleLegacyMessage),
    onProfile: guarded(handleProfile),
    onAlert: guarded(handleAlert),
    onEnd: guarded(handleEnd),
    onError: guarded(handleError),
    onStatus: (state) => {
      if (!isCurrent()) return
      connState.value = state
      // 连接就绪后再发送开始授课指令：避免固定 setTimeout 在慢网络下早于 onopen 而丢失 start
      if (state === 'connected' && !startSent) {
        startSent = true
        instance.sendStart()
      }
    },
  })
  socket.value = instance
  instance.connect()
}

function resetClassroomState() {
  resetMessages()
  alerts.value = []
  profile.value = null
  currentPhase.value = ''
  currentRound.value = 0
  ended.value = false
  autoFollow.value = true
  // 清空上一堂课的 Agent 工具演示结果（面板工具列表缓存保留，避免重复请求）
  toolAgentRef.value?.reset()
}

/** 创建会话并连接 WebSocket，随后发送"开始授课"指令 */
async function handleStart() {
  const trimmed = topic.value.trim()
  if (!trimmed) {
    ElMessage.warning('请先输入授课主题')
    return
  }
  if (starting.value) return
  starting.value = true
  try {
    // 1. REST 创建课堂会话
    const { session_id } = await startClassroom(trimmed)
    sessionId.value = session_id
    // 2. 新课堂：清空上一堂课的本地展示状态，避免新旧课堂消息/阶段/测验/档案混在一起
    socket.value?.close()
    resetClassroomState()
    // 3. 建立 WebSocket 并启动授课
    connectSocket(session_id)
  } catch (err) {
    ElMessage.error(err.message || '创建会话失败')
  } finally {
    starting.value = false
  }
}

onBeforeUnmount(() => {
  if (scrollRaf != null) cancelAnimationFrame(scrollRaf)
  socket.value?.close()
})
</script>

<style scoped>
.classroom-page {
  max-width: 1200px;
  margin: 0 auto;
  padding: 16px;
}

.classroom-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  padding: 12px 4px;
}

.header-tags {
  display: flex;
  align-items: center;
  gap: 8px;
}

.title {
  font-size: 20px;
  color: #303133;
}

.classroom-body {
  display: flex;
  gap: 16px;
  align-items: flex-start;
}

.control-panel {
  width: 340px;
  flex-shrink: 0;
}

.status-block {
  padding: 0 4px;
}

.status-row {
  display: flex;
  justify-content: space-between;
  padding: 4px 0;
  font-size: 13px;
}

.status-label {
  color: #909399;
}

.status-value {
  color: #303133;
  max-width: 200px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.chat-panel {
  flex: 1;
  min-width: 0;
  background: #fff;
  border-radius: 8px;
  border: 1px solid #e4e7ed;
  padding: 12px;
  min-height: 70vh;
}

.alert-bar {
  margin-bottom: 12px;
}

.chat-list {
  height: calc(70vh - 40px);
  overflow-y: auto;
  padding: 8px 4px;
}

.profile-panel {
  margin-top: 16px;
}

@media (max-width: 960px) {
  .classroom-body {
    flex-direction: column;
    align-items: stretch;
  }

  .control-panel {
    width: 100%;
  }

  .chat-list {
    height: 60vh;
  }
}
</style>
