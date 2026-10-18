<template>
  <div class="result-block">
    <!-- 运行元信息：是否降级 / MCP 状态 / 步数 / 总耗时 / 工具调用次数 -->
    <div class="result-meta">
      <el-tag
        size="small"
        :type="result.degraded ? 'warning' : 'success'"
        :effect="result.degraded ? 'dark' : 'plain'"
      >
        {{ result.degraded ? '降级返回' : '正常完成' }}
      </el-tag>
      <el-tag size="small" :type="result.mcp_connected ? 'success' : 'info'" effect="plain">
        MCP {{ result.mcp_connected ? '已连接' : '未连接' }}
      </el-tag>
      <span class="meta-item">步数：{{ result.steps ?? '-' }}</span>
      <span class="meta-item">总耗时：{{ formatMilliseconds(result.client_elapsed_ms) }}</span>
      <span class="meta-item">工具调用：{{ trace.length }} 次</span>
    </div>

    <!-- 最终回答 -->
    <div class="answer-block">
      <div class="block-title">回答</div>
      <div class="answer-text">{{ result.answer || '（空回答）' }}</div>
    </div>

    <!-- 使用到的工具 chips -->
    <div v-if="toolsUsed.length" class="used-block">
      <div class="block-title">使用工具</div>
      <div class="tag-list">
        <el-tag v-for="name in toolsUsed" :key="name" size="small" type="warning" effect="plain">
          {{ name }}
        </el-tag>
      </div>
    </div>

    <!-- ReAct 调用轨迹：每步工具 / 参数 / 观察（截断） / 耗时 -->
    <div class="trace-block">
      <div class="block-title">调用轨迹</div>
      <el-empty v-if="!trace.length" description="本次未调用工具，Agent 直接作答" :image-size="40" />
      <el-timeline v-else>
        <el-timeline-item
          v-for="(item, index) in trace"
          :key="index"
          :timestamp="`第 ${item.step ?? index + 1} 步 · ${formatMilliseconds(item.elapsed_ms)}`"
          placement="top"
          type="primary"
          hollow
        >
          <div class="trace-item">
            <div class="trace-head">
              <el-tag size="small" type="warning" effect="dark">{{ item.tool || 'unknown' }}</el-tag>
            </div>
            <div v-if="argsText(item.args)" class="trace-line">
              <span class="trace-label">参数</span>
              <code class="trace-code">{{ argsText(item.args) }}</code>
            </div>
            <div class="trace-line">
              <span class="trace-label">观察</span>
              <span class="trace-observation" :title="fullObservation(item)">
                {{ truncateText(item.observation, 200) }}
              </span>
            </div>
          </div>
        </el-timeline-item>
      </el-timeline>
    </div>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { formatMilliseconds, truncateText } from '../utils/format.js'

const props = defineProps({
  // POST /api/agent/ask 返回体 { answer, trace, steps, degraded, mcp_connected, tools_used }
  // 另含前端补充的 client_elapsed_ms（整体耗时，接口本身不返回）
  result: { type: Object, required: true },
})

const trace = computed(() => (Array.isArray(props.result?.trace) ? props.result.trace : []))
const toolsUsed = computed(() => (Array.isArray(props.result?.tools_used) ? props.result.tools_used : []))

/** 工具参数对象 → "key=value, key=value" 短文本（超长截断） */
function argsText(args) {
  if (!args || typeof args !== 'object') return ''
  const entries = Object.entries(args)
  if (!entries.length) return ''
  const text = entries
    .map(([key, value]) => `${key}=${typeof value === 'string' ? value : JSON.stringify(value)}`)
    .join(', ')
  return truncateText(text, 120)
}

/** 完整 observation，用于 title 提示（列表里只展示截断文本） */
function fullObservation(item) {
  return String(item?.observation ?? '')
}
</script>

<style scoped>
.result-block {
  margin-top: 12px;
}

.result-meta {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px;
  margin-bottom: 10px;
}

.meta-item {
  font-size: 12px;
  color: #606266;
}

.answer-block {
  padding: 10px 12px;
  margin-bottom: 12px;
  background: #f0f9eb;
  border: 1px solid #e1f3d8;
  border-radius: 8px;
}

.block-title {
  margin-bottom: 6px;
  font-size: 13px;
  font-weight: 600;
  color: #303133;
}

.answer-text {
  font-size: 14px;
  line-height: 1.7;
  color: #2d4a1d;
  white-space: pre-wrap;
  word-break: break-word;
}

.used-block {
  margin-bottom: 12px;
}

.tag-list {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.trace-block {
  margin-top: 4px;
}

.trace-item {
  font-size: 13px;
}

.trace-head {
  margin-bottom: 4px;
}

.trace-line {
  display: flex;
  gap: 8px;
  margin-bottom: 2px;
  line-height: 1.6;
}

.trace-label {
  flex-shrink: 0;
  min-width: 28px;
  font-size: 12px;
  color: #909399;
}

.trace-code {
  padding: 0 6px;
  font-size: 12px;
  color: #476582;
  background: #f5f7fa;
  border-radius: 4px;
  word-break: break-all;
}

.trace-observation {
  color: #606266;
  white-space: pre-wrap;
  word-break: break-word;
}
</style>
