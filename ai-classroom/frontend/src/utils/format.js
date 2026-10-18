/**
 * 展示层格式化工具：阶段/性格标签、时长与百分比、选项字母、学生名兜底。
 *
 * 与后端契约对齐（见 backend/graph/workflow.py、analytics.py、config.py）：
 * - phase 取值 teaching / quiz / answer / discuss / grading / summary / end；
 * - personality 取值 introvert / active / expert / follower；
 * - 学生 id 形如 student_1（后端默认姓名为 同学1）；
 * - accuracy / student_ratio 后端给 0-1 小数，统一按百分比展示。
 */

export const PHASE_LABELS = {
  teaching: '授课中',
  quiz: '随堂提问',
  answer: '课堂答疑',
  discuss: '小组研讨',
  grading: '作业批改',
  summary: '学情总结',
  end: '已结束',
}

export const PERSONALITY_LABELS = {
  introvert: '内向型',
  active: '活跃型',
  expert: '学霸型',
  follower: '跟随型',
}

/** 课堂阶段展示名；未知阶段原样返回，空值返回空串 */
export function phaseLabel(phase) {
  return phase ? PHASE_LABELS[phase] || String(phase) : ''
}

/** 学生性格展示名 */
export function personalityLabel(personality) {
  return personality ? PERSONALITY_LABELS[personality] || String(personality) : '-'
}

/** 选项下标（0-3…）→ 选项字母（A-D…），非法下标返回 "?" */
export function optionLetter(index) {
  const n = Number(index)
  if (!Number.isInteger(n) || n < 0 || n > 25) return '?'
  return String.fromCharCode(65 + n)
}

/** 秒数 → 中文可读时长（用于课堂时长 / 环节超时告警） */
export function formatDuration(seconds) {
  if (seconds == null || seconds === '') return '-'
  const n = Number(seconds)
  if (!Number.isFinite(n) || n < 0) return '-'
  const total = Math.round(n)
  if (total < 60) return `${total} 秒`
  const minutes = Math.floor(total / 60)
  const rest = total % 60
  return rest ? `${minutes} 分 ${rest} 秒` : `${minutes} 分钟`
}

/** 0-1 小数（或已是百分数）→ 百分比文本 */
export function formatPercent(value) {
  if (value == null || value === '') return '-'
  const n = Number(value)
  if (!Number.isFinite(n)) return '-'
  const percent = n <= 1 ? n * 100 : n
  return `${Math.round(percent)}%`
}

/**
 * 学生 id 兜底显示名：后端学生 id 形如 student_1、默认姓名 同学1。
 * 收到 profile 帧后优先展示档案里的真实姓名（见 Classroom.vue 的 studentNameMap）。
 */
export function fallbackStudentName(studentId) {
  const text = String(studentId || '')
  const matched = /^student_(\d+)$/.exec(text)
  return matched ? `同学${matched[1]}` : text
}

/** 去掉选项文本自带的 "A. "/"A、"/"A)" 前缀，避免与选项字母标签重复 */
export function cleanOptionText(option, index) {
  const text = String(option ?? '').trim()
  if (!text) return ''
  const letter = optionLetter(index)
  if (letter === '?') return text
  const prefixed = new RegExp(`^${letter}\\s*[.、)）．:：]\\s*`, 'i')
  const stripped = text.replace(prefixed, '').trim()
  return stripped || text
}
