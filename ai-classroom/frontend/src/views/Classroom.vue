<template>
  <div class="classroom-page">
    <!-- 顶栏：标题与连接状态 -->
    <header class="classroom-header">
      <h1 class="title">AI Classroom 多智能体互动教学课堂</h1>
      <el-tag :type="connTagType" size="large">{{ connText }}</el-tag>
    </header>

    <div class="classroom-body">
      <!-- 左侧：控制面板 -->
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
          </div>

          <el-divider />

          <!-- 答题/作业面板（骨架占位，后续扩展） -->
          <el-empty description="答题 / 作业面板（待扩展）" :image-size="60" />
        </el-card>
      </aside>

      <!-- 右侧：实时消息流 -->
      <main class="chat-panel">
        <el-alert
          v-if="alerts.length"
          :title="latestAlertText"
          type="warning"
          :closable="false"
          show-icon
          class="alert-bar"
        />
        <div ref="chatListRef" class="chat-list">
          <el-empty v-if="!messages.length" description="发起授课后，课堂消息将实时展示在这里" />
          <MessageBubble
            v-for="(msg, index) in messages"
            :key="index"
            :speaker="msg.speaker"
            :content="msg.content"
            :citation="msg.citation"
            :time="msg.time"
          />
        </div>
      </main>
    </div>
  </div>
</template>

<script setup>
import { computed, nextTick, ref } from 'vue'
import { ElMessage } from 'element-plus'
import MessageBubble from '../components/MessageBubble.vue'
import ClassroomSocket from '../websocket/index.js'
import { startClassroom } from '../api/index.js'

// 阶段展示映射（与后端 workflow.py 中各节点上报的 phase 一致）
const PHASE_TEXT = {
  teaching: '授课中',
  quiz: '随堂提问',
  answer: '课堂答疑',
  discuss: '小组研讨',
  grading: '作业批改',
  summary: '学情总结',
  end: '已结束',
}

const topic = ref('')
const sessionId = ref('')
const socket = ref(null)
const starting = ref(false)
const messages = ref([])
const alerts = ref([])
const connState = ref('idle') // idle | connecting | connected | disconnected
const currentPhase = ref('') // 最近一次消息上报的阶段（学生消息缺少该字段时不会回退）
const currentRound = ref(0)
const chatListRef = ref(null)
// 本会话是否已发送过「开始授课」指令（重连时不再重复发送）
let startSent = false

const connText = computed(() => {
  return { idle: '未连接', connecting: '连接中', connected: '已连接', disconnected: '已断开' }[connState.value]
})
const connTagType = computed(() => {
  return { idle: 'info', connecting: 'warning', connected: 'success', disconnected: 'danger' }[connState.value]
})
const phaseText = computed(() => PHASE_TEXT[currentPhase.value] || '待开始')
const roundInfo = computed(() => (currentRound.value ? `第 ${currentRound.value} 轮` : '-'))
const latestAlertText = computed(() => {
  const last = alerts.value[alerts.value.length - 1]
  return last ? `秩序提醒：${last.student || last.type || ''}` : ''
})

/** 创建会话并连接 WebSocket，随后发送"开始授课"指令 */
async function handleStart() {
  if (!topic.value.trim()) {
    ElMessage.warning('请先输入授课主题')
    return
  }
  starting.value = true
  try {
    // 1. REST 创建课堂会话
    const { session_id } = await startClassroom(topic.value.trim())
    sessionId.value = session_id
    // 2. 新课堂：清空上一堂课的本地展示状态，避免新旧课堂消息/阶段混在一起
    socket.value?.close()
    messages.value = []
    alerts.value = []
    currentPhase.value = ''
    currentRound.value = 0
    // 3. 建立 WebSocket 并启动授课
    connectSocket(session_id)
  } catch (err) {
    ElMessage.error(err.message || '创建会话失败')
  } finally {
    starting.value = false
  }
}

function connectSocket(sessionId) {
  socket.value?.close()
  startSent = false
  socket.value = new ClassroomSocket(sessionId, {
    onMessage: (data) => {
      // 学生/仲裁器消息也带有后端回填的 round/phase；收到即更新状态栏
      if (data.phase) currentPhase.value = data.phase
      if (data.round != null) currentRound.value = data.round
      messages.value.push({
        speaker: data.speaker,
        content: data.content,
        citation: data.citation || [],
        phase: data.phase,
        round: data.round,
        time: new Date().toLocaleTimeString('zh-CN', { hour12: false }),
      })
      scrollToBottom()
    },
    onAlert: (data) => {
      alerts.value.push(data.alert || {})
      ElMessage.warning(`秩序提醒：${data.alert?.student || ''}`)
    },
    onEnd: () => {
      currentPhase.value = 'end'
      ElMessage.success('课堂流程已结束')
    },
    onError: (data) => {
      ElMessage.error(data.content || '课堂运行异常')
    },
    onStatus: (state) => {
      connState.value = state
      // 连接就绪后再发送开始授课指令：避免固定 setTimeout 在慢网络下早于 onopen 而丢失 start
      if (state === 'connected' && !startSent) {
        startSent = true
        socket.value.sendStart()
      }
    },
  })
  socket.value.connect()
}

function scrollToBottom() {
  nextTick(() => {
    if (chatListRef.value) {
      chatListRef.value.scrollTop = chatListRef.value.scrollHeight
    }
  })
}
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
  padding: 12px 4px;
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
  width: 320px;
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
</style>
