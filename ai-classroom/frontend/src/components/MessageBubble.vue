<template>
  <div class="message-bubble" :class="bubbleClass">
    <div class="bubble-header">
      <span class="speaker-name">{{ speakerLabel }}</span>
      <span v-if="time" class="bubble-time">{{ time }}</span>
    </div>
    <div class="bubble-content">{{ content }}</div>
    <div v-if="citation && citation.length" class="bubble-citation">
      来源：{{ citation.join('；') }}
    </div>
  </div>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
  speaker: { type: String, default: '' },
  content: { type: String, default: '' },
  citation: { type: Array, default: () => [] },
  time: { type: String, default: '' },
})

// 角色显示名映射
const SPEAKER_LABELS = {
  teacher: '主讲老师',
  ta: '助教',
  manager: '课堂管理员',
}

const speakerLabel = computed(() => SPEAKER_LABELS[props.speaker] || props.speaker)
const bubbleClass = computed(() => `bubble-${props.speaker || 'unknown'}`)
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
  margin-bottom: 4px;
}

.speaker-name {
  font-weight: 600;
  font-size: 13px;
}

.bubble-time {
  color: #909399;
  font-size: 12px;
}

.bubble-citation {
  margin-top: 6px;
  font-size: 12px;
  color: #909399;
  border-top: 1px dashed #dcdfe6;
  padding-top: 4px;
}
</style>
