<template>
  <div class="message-bubble" :class="bubbleClass">
    <div class="bubble-header">
      <div class="speaker-line">
        <span class="speaker-name">{{ speakerLabelText }}</span>
        <el-tag v-if="groupName" size="small" type="info" effect="plain" class="bubble-tag">
          分组：{{ groupName }}<template v-if="groupAngle"> · {{ groupAngle }}</template>
        </el-tag>
        <el-tag v-if="quiz" size="small" type="danger" effect="dark" class="bubble-tag">随堂测验</el-tag>
        <span v-if="quiz && quiz.knowledge_point" class="quiz-kp">知识点：{{ quiz.knowledge_point }}</span>
      </div>
      <div class="bubble-meta">
        <span v-if="round" class="bubble-round">第 {{ round }} 轮</span>
        <span v-if="time" class="bubble-time">{{ time }}</span>
      </div>
    </div>

    <div class="bubble-content">{{ content }}<span v-if="streaming" class="typing-cursor" aria-hidden="true"></span></div>

    <div v-if="answer" class="bubble-answer">
      <el-tag size="small" type="warning" effect="dark" class="answer-tag">选 {{ answerLetter }}</el-tag>
      <span v-if="answer.choice_text" class="answer-text">{{ answer.choice_text }}</span>
    </div>

    <div v-if="citation && citation.length" class="bubble-citation">
      来源：{{ citation.join('；') }}
    </div>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { optionLetter } from '../utils/format.js'

const props = defineProps({
  speaker: { type: String, default: '' },
  // 外部解析后的显示名（学生姓名等），优先级高于内置角色映射
  speakerLabel: { type: String, default: '' },
  // message_delta 逐帧追加后的内容；message_end 收尾为完整内容
  content: { type: String, default: '' },
  citation: { type: Array, default: () => [] },
  time: { type: String, default: '' },
  phase: { type: String, default: '' },
  round: { type: Number, default: 0 },
  // 学生研讨分组：{ group_id, name, angle, members, member_ids }
  group: { type: Object, default: null },
  // 学生作答：{ quiz_id, student_id, choice_index, choice_text }
  answer: { type: Object, default: null },
  // 老师随堂测验：{ quiz_id, question, options, answer_index, knowledge_point }
  quiz: { type: Object, default: null },
  // true 时展示闪烁光标（流式输出中）
  streaming: { type: Boolean, default: false },
})

// 角色显示名映射（学生显示名由父组件通过 speakerLabel 解析）
const SPEAKER_LABELS = {
  teacher: '主讲老师',
  ta: '助教',
  manager: '课堂管理员',
}

const speakerLabelText = computed(() => {
  if (props.speakerLabel) return props.speakerLabel
  const speaker = props.speaker || ''
  return SPEAKER_LABELS[speaker] || speaker
})
// 学生 id 形如 student_1/student_2…，统一映射到 .bubble-student 样式
const bubbleClass = computed(() => {
  const speaker = props.speaker || ''
  if (speaker.startsWith('student_')) return 'bubble-student'
  return `bubble-${speaker || 'unknown'}`
})
const groupName = computed(() => {
  const group = props.group
  if (!group) return ''
  return group.name || group.group_id || ''
})
const groupAngle = computed(() => props.group?.angle || '')
const answerLetter = computed(() => optionLetter(props.answer?.choice_index))
</script>

<style scoped>
.message-bubble {
  max-width: 78%;
  padding: 10px 14px;
  border-radius: 10px;
  margin-bottom: 12px;
  line-height: 1.6;
  font-size: 14px;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.08);
}

/* 老师：居中强调 */
.bubble-teacher {
  margin-left: auto;
  margin-right: auto;
  background: #fdf6ec;
  border: 1px solid #e6a23c;
  color: #5c3d0e;
}

/* 助教：左侧 */
.bubble-ta {
  background: #f0f9eb;
  border: 1px solid #85ce61;
  color: #2d4a1d;
}

/* 学生：右侧 */
.bubble-student {
  margin-left: auto;
  background: #ecf5ff;
  border: 1px solid #79bbff;
  color: #1e3a5f;
}

/* 管理员：左侧 */
.bubble-manager {
  background: #f4f4f5;
  border: 1px solid #c0c4cc;
  color: #333;
}

.bubble-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 8px;
  margin-bottom: 4px;
}

.speaker-line {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  min-width: 0;
}

.speaker-name {
  font-weight: 600;
  font-size: 13px;
}

.bubble-tag {
  flex-shrink: 0;
}

.quiz-kp {
  font-size: 12px;
  color: #b88230;
}

.bubble-meta {
  display: flex;
  flex-shrink: 0;
  align-items: center;
  gap: 8px;
}

.bubble-round {
  color: #909399;
  font-size: 12px;
}

.bubble-time {
  color: #909399;
  font-size: 12px;
}

.bubble-content {
  white-space: pre-wrap;
  word-break: break-word;
}

/* 流式输出光标：CSS 闪烁，配合 message_delta 逐帧追加形成打字机效果 */
.typing-cursor {
  display: inline-block;
  width: 2px;
  height: 1em;
  margin-left: 2px;
  vertical-align: -0.15em;
  background: currentColor;
  animation: typing-cursor-blink 1s steps(1, end) infinite;
}

@keyframes typing-cursor-blink {
  0%,
  49% {
    opacity: 1;
  }
  50%,
  100% {
    opacity: 0;
  }
}

.bubble-answer {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  margin-top: 6px;
}

.answer-tag {
  flex-shrink: 0;
}

.answer-text {
  font-size: 12px;
  color: #606266;
}

.bubble-citation {
  margin-top: 6px;
  font-size: 12px;
  color: #909399;
  border-top: 1px dashed #dcdfe6;
  padding-top: 4px;
}

@media (max-width: 768px) {
  .message-bubble {
    max-width: 94%;
  }
}
</style>
