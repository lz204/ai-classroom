<template>
  <el-card class="profile-card" shadow="never">
    <template #header>
      <div class="profile-header">
        <div class="profile-title">
          <span class="title-icon">📊</span>
          <span>学情档案</span>
          <el-tag v-if="profile.topic" size="small" type="success" effect="plain">{{ profile.topic }}</el-tag>
        </div>
        <el-tag size="small" type="info" effect="plain">课堂已结束</el-tag>
      </div>
    </template>

    <!-- 课堂概览 -->
    <div class="metric-grid">
      <div v-for="metric in metrics" :key="metric.label" class="metric-item">
        <div class="metric-value">{{ metric.value }}</div>
        <div class="metric-label">{{ metric.label }}</div>
      </div>
    </div>

    <!-- 学生表现 -->
    <el-divider content-position="left">学生表现</el-divider>
    <div class="table-wrap">
      <el-table :data="studentRows" size="small" border stripe empty-text="暂无学生数据">
        <el-table-column prop="name" label="姓名" min-width="90" />
        <el-table-column prop="personality" label="性格" width="90" />
        <el-table-column prop="group" label="分组" width="100" />
        <el-table-column prop="messages" label="发言" width="70" align="center" />
        <el-table-column prop="questions" label="提问" width="70" align="center" />
        <el-table-column prop="answers" label="答题" width="70" align="center" />
        <el-table-column prop="correct" label="正确" width="70" align="center" />
        <el-table-column prop="timeouts" label="超时" width="70" align="center" />
      </el-table>
    </div>

    <!-- 随堂测验 -->
    <el-divider content-position="left">随堂测验</el-divider>
    <div class="quiz-block">
      <div class="quiz-question">{{ quiz.question || '本堂课未发起随堂测验' }}</div>
      <div class="quiz-stats">
        <span class="quiz-stat">作答人数：<strong>{{ quiz.total_answered ?? 0 }}</strong></span>
        <template v-if="accuracyPercent !== null">
          <span class="quiz-stat">正确率：</span>
          <el-progress
            class="quiz-progress"
            :percentage="accuracyPercent"
            :stroke-width="10"
            :color="accuracyPercent >= 60 ? '#67c23a' : '#e6a23c'"
          />
        </template>
        <span v-else class="quiz-stat muted">正确率：暂无作答数据</span>
      </div>
    </div>

    <!-- 知识点与薄弱点 -->
    <el-divider content-position="left">知识点与薄弱点</el-divider>
    <div class="points-grid">
      <div class="points-block">
        <div class="points-title">覆盖知识点</div>
        <div class="tag-list">
          <el-tag v-for="point in knowledgePoints" :key="point" size="small" type="success" effect="plain">
            {{ point }}
          </el-tag>
          <span v-if="!knowledgePoints.length" class="muted">暂无</span>
        </div>
      </div>
      <div class="points-block">
        <div class="points-title">薄弱点</div>
        <div class="tag-list">
          <el-tag v-for="point in weakPoints" :key="point" size="small" type="danger" effect="plain">
            {{ point }}
          </el-tag>
          <span v-if="!weakPoints.length" class="muted">暂无明显薄弱点</span>
        </div>
      </div>
    </div>

    <!-- 小组研讨 -->
    <template v-if="groups.length">
      <el-divider content-position="left">小组研讨</el-divider>
      <div class="group-grid">
        <div v-for="group in groups" :key="group.key" class="group-item">
          <div class="group-name">{{ group.name }}</div>
          <div v-if="group.angle" class="group-angle">讨论角度：{{ group.angle }}</div>
          <div class="group-members">成员：{{ group.membersText }}</div>
          <div class="group-messages">发言 {{ group.messages }} 条</div>
        </div>
      </div>
    </template>
  </el-card>
</template>

<script setup>
import { computed } from 'vue'
import { formatDuration, formatPercent, personalityLabel } from '../utils/format.js'

const props = defineProps({
  // 学情档案（WS profile 帧下发的 profile 字段，见 backend/analytics.py::finalize）
  profile: { type: Object, required: true },
})

function numberOrDash(value) {
  if (value == null || value === '') return '-'
  const n = Number(value)
  return Number.isFinite(n) ? n : '-'
}

function groupNameOf(group) {
  if (!group) return '-'
  if (typeof group === 'string') return group
  return group.name || group.group_id || '-'
}

const metrics = computed(() => {
  const profile = props.profile || {}
  const interaction = profile.interaction || {}
  const items = [
    { label: '课堂时长', value: formatDuration(profile.duration_seconds) },
    { label: '课堂轮次', value: numberOrDash(profile.rounds) },
    { label: '消息总数', value: numberOrDash(profile.messages) },
    { label: '学生发言', value: numberOrDash(interaction.student_messages) },
    { label: '老师发言', value: numberOrDash(interaction.teacher_messages) },
  ]
  // 助教/管理员发言数后端可能未提供，存在时才展示
  if (interaction.ta_messages != null) {
    items.push({ label: '助教发言', value: numberOrDash(interaction.ta_messages) })
  }
  if (interaction.manager_messages != null) {
    items.push({ label: '管理员发言', value: numberOrDash(interaction.manager_messages) })
  }
  items.push(
    { label: '秩序告警', value: numberOrDash(interaction.alerts) },
    { label: '学生发言占比', value: formatPercent(interaction.student_ratio) },
  )
  return items
})

const studentRows = computed(() => {
  const students = Array.isArray(props.profile?.students) ? props.profile.students : []
  return students.map((student, index) => ({
    key: student.id || `student-${index}`,
    name: student.name || student.id || '未知学生',
    personality: personalityLabel(student.personality),
    group: groupNameOf(student.group),
    messages: numberOrDash(student.messages),
    questions: numberOrDash(student.questions),
    answers: numberOrDash(student.answers),
    correct: numberOrDash(student.correct),
    timeouts: numberOrDash(student.timeouts),
  }))
})

const quiz = computed(() => props.profile?.quiz || {})

const accuracyPercent = computed(() => {
  const raw = quiz.value.accuracy
  if (raw == null) return null
  const n = Number(raw)
  if (!Number.isFinite(n)) return null
  const percent = n <= 1 ? n * 100 : n
  return Math.max(0, Math.min(100, Math.round(percent)))
})

const knowledgePoints = computed(() =>
  (Array.isArray(props.profile?.knowledge_points) ? props.profile.knowledge_points : []).filter(Boolean),
)

const weakPoints = computed(() =>
  (Array.isArray(props.profile?.weak_points) ? props.profile.weak_points : []).filter(Boolean),
)

const groups = computed(() => {
  const list = Array.isArray(props.profile?.groups) ? props.profile.groups : []
  return list.map((group, index) => ({
    key: group.group_id || group.name || `group-${index}`,
    name: group.name || group.group_id || `第 ${index + 1} 组`,
    angle: group.angle || '',
    membersText: (Array.isArray(group.members) ? group.members : []).join('、') || '-',
    messages: numberOrDash(group.messages),
  }))
})
</script>

<style scoped>
.profile-card {
  margin-top: 16px;
}

.profile-header {
  display: flex;
  flex-wrap: wrap;
  justify-content: space-between;
  align-items: center;
  gap: 8px;
}

.profile-title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 16px;
  font-weight: 600;
  color: #303133;
}

.title-icon {
  font-size: 18px;
}

.metric-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
  gap: 10px;
}

.metric-item {
  padding: 10px 12px;
  border-radius: 8px;
  background: #f5f7fa;
  text-align: center;
}

.metric-value {
  font-size: 18px;
  font-weight: 600;
  color: #409eff;
  line-height: 1.4;
}

.metric-label {
  font-size: 12px;
  color: #909399;
  margin-top: 2px;
}

.table-wrap {
  width: 100%;
  overflow-x: auto;
}

.quiz-block {
  padding: 0 2px;
}

.quiz-question {
  color: #303133;
  margin-bottom: 8px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
}

.quiz-stats {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px;
  font-size: 13px;
  color: #606266;
}

.quiz-progress {
  width: 180px;
}

.points-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
  gap: 12px;
}

.points-block {
  padding: 10px 12px;
  border: 1px solid #ebeef5;
  border-radius: 8px;
  background: #fafafa;
}

.points-title {
  font-weight: 600;
  font-size: 13px;
  color: #303133;
  margin-bottom: 8px;
}

.tag-list {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.group-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 12px;
}

.group-item {
  padding: 10px 12px;
  border: 1px solid #ebeef5;
  border-radius: 8px;
  background: #fafafa;
  font-size: 13px;
  color: #606266;
  line-height: 1.7;
}

.group-name {
  font-weight: 600;
  color: #303133;
  margin-bottom: 2px;
}

.group-angle {
  color: #b88230;
}

.muted {
  color: #909399;
  font-size: 12px;
}

@media (max-width: 768px) {
  .metric-grid {
    grid-template-columns: repeat(auto-fit, minmax(96px, 1fr));
  }

  .quiz-progress {
    width: 140px;
  }
}
</style>
