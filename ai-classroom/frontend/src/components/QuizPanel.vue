<template>
  <div class="quiz-panel">
    <template v-if="quiz">
      <div class="quiz-head">
        <el-tag size="small" type="danger" effect="dark">随堂测验</el-tag>
        <span v-if="quiz.knowledge_point" class="quiz-kp">知识点：{{ quiz.knowledge_point }}</span>
      </div>

      <div class="quiz-question">{{ quiz.question || '题目生成中…' }}</div>

      <ul v-if="options.length" class="quiz-options">
        <li
          v-for="(option, index) in options"
          :key="index"
          class="quiz-option"
          :class="{ 'is-correct': showCorrect && index === answerIndex }"
        >
          <span class="option-letter">{{ optionLetter(index) }}</span>
          <span class="option-text">{{ cleanOptionText(option, index) }}</span>
          <span v-if="counts[index]" class="option-count">{{ counts[index] }} 人</span>
        </li>
      </ul>

      <div class="quiz-answers">
        <div class="answers-title">
          <span>学生作答（{{ rows.length }} 人）</span>
          <el-tag v-if="showCorrect" size="small" type="success" effect="plain">
            正确选项：{{ optionLetter(answerIndex) }}
          </el-tag>
        </div>
        <el-empty v-if="!rows.length" description="等待学生作答" :image-size="44" />
        <ul v-else class="answer-list">
          <li v-for="row in rows" :key="row.key" class="answer-item">
            <span class="answer-name">{{ row.studentName }}</span>
            <el-tag size="small" :type="row.tagType" effect="plain">{{ row.label }}</el-tag>
          </li>
        </ul>
      </div>
    </template>

    <el-empty v-else description="暂无随堂测验，老师发起提问后展示" :image-size="60" />
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { cleanOptionText, fallbackStudentName, optionLetter } from '../utils/format.js'

const props = defineProps({
  // 当前随堂测验：{ quiz_id, question, options, answer_index, knowledge_point }
  quiz: { type: Object, default: null },
  // 学生作答原始帧：{ quiz_id, student_id, choice_index, choice_text }（可能重复，组件内去重）
  answers: { type: Array, default: () => [] },
  // 学生 id -> 显示姓名（来自学情档案）；缺失时用「同学N」兜底
  nameMap: { type: Object, default: () => ({}) },
})

const options = computed(() => {
  const list = props.quiz?.options
  return Array.isArray(list) ? list : []
})

const answerIndex = computed(() => {
  const raw = props.quiz?.answer_index
  if (raw == null) return null
  const index = Number(raw)
  return Number.isInteger(index) && index >= 0 ? index : null
})

// 按学生去重（同一学生若重复补发，以最后一次为准）
const rows = computed(() => {
  const raw = Array.isArray(props.answers) ? props.answers : []
  const byStudent = new Map()
  for (const item of raw) {
    if (!item) continue
    const studentId = item.student_id || item.studentId || ''
    const choiceIndexRaw = item.choice_index ?? item.choiceIndex
    const choiceIndex = Number(choiceIndexRaw)
    const choiceText = String(item.choice_text ?? item.choiceText ?? '').trim()
    const key = studentId || `anonymous-${byStudent.size}`
    byStudent.set(key, {
      key,
      studentId,
      studentName: props.nameMap?.[studentId] || fallbackStudentName(studentId) || '匿名学生',
      choiceIndex: Number.isInteger(choiceIndex) && choiceIndex >= 0 ? choiceIndex : null,
      choiceText,
    })
  }

  const correctIndex = answerIndex.value
  return [...byStudent.values()].map((row) => {
    const letter = row.choiceIndex == null ? '?' : optionLetter(row.choiceIndex)
    const text = row.choiceText
      ? cleanOptionText(row.choiceText, row.choiceIndex == null ? 0 : row.choiceIndex)
      : ''
    let tagType = 'info'
    if (correctIndex != null && row.choiceIndex != null) {
      tagType = row.choiceIndex === correctIndex ? 'success' : 'danger'
    }
    return {
      ...row,
      tagType,
      label: text ? `${letter} · ${text}` : letter === '?' ? '已作答' : `选 ${letter}`,
    }
  })
})

// 每个选项被选择的人数
const counts = computed(() => options.value.map((_, index) => rows.value.filter((row) => row.choiceIndex === index).length))

// 有作答后才展示正确答案，避免提前泄题
const showCorrect = computed(() => answerIndex.value != null && rows.value.length > 0)
</script>

<style scoped>
.quiz-panel {
  font-size: 13px;
}

.quiz-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  margin-bottom: 8px;
}

.quiz-kp {
  font-size: 12px;
  color: #b88230;
}

.quiz-question {
  color: #303133;
  line-height: 1.6;
  margin-bottom: 8px;
  white-space: pre-wrap;
  word-break: break-word;
}

.quiz-options {
  list-style: none;
  margin: 0 0 10px;
  padding: 0;
}

.quiz-option {
  display: flex;
  align-items: flex-start;
  gap: 6px;
  padding: 5px 8px;
  margin-bottom: 4px;
  border: 1px solid #e4e7ed;
  border-radius: 6px;
  background: #fafafa;
  color: #606266;
  line-height: 1.5;
}

.quiz-option.is-correct {
  border-color: #67c23a;
  background: #f0f9eb;
  color: #2d4a1d;
}

.option-letter {
  flex-shrink: 0;
  width: 18px;
  height: 18px;
  line-height: 18px;
  text-align: center;
  border-radius: 50%;
  background: #dcdfe6;
  color: #303133;
  font-size: 12px;
  font-weight: 600;
}

.quiz-option.is-correct .option-letter {
  background: #67c23a;
  color: #fff;
}

.option-text {
  flex: 1;
  min-width: 0;
  word-break: break-word;
}

.option-count {
  flex-shrink: 0;
  color: #909399;
  font-size: 12px;
}

.answers-title {
  display: flex;
  flex-wrap: wrap;
  justify-content: space-between;
  align-items: center;
  gap: 6px;
  margin-bottom: 6px;
  color: #909399;
  font-size: 12px;
}

.answer-list {
  list-style: none;
  margin: 0;
  padding: 0;
  max-height: 180px;
  overflow-y: auto;
}

.answer-item {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 8px;
  padding: 4px 0;
  border-bottom: 1px dashed #ebeef5;
}

.answer-item:last-child {
  border-bottom: none;
}

.answer-name {
  color: #303133;
}

.answer-item .el-tag {
  max-width: 70%;
  overflow: hidden;
  text-overflow: ellipsis;
}
</style>
