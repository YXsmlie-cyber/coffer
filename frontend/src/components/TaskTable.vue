<script setup>
import { computed, ref, watch, onMounted, onUnmounted, nextTick } from 'vue'
import api from '../api'
import { alert, confirm } from '../composables/useDialog'
import { failureDetail, failureReason } from '../utils/failureReason.js'
import { apiErrorText } from '../utils/apiError'
import { etaText, formatBytes, formatDateTime, formatSpeed, untilText } from '../utils/formatters'
import { progressTime } from '../utils/progressTime'

const props = defineProps({
  tasks: { type: Array, default: () => [] },
  loading: { type: Boolean, default: false },
  listGeneration: { type: Number, default: 0 },
  emptyText: { type: String, default: '暂无任务' },
  emptyHint: { type: String, default: '' },
  emptyTone: { type: String, default: '' },
})

const emit = defineEmits(['change', 'refresh'])

const statusText = {
  queued: '排队', running: '进行中', paused: '已暂停',
  completed: '已完成', failed: '失败', skipped: '跳过',
}

// 与后端 MEDIA_TOOLS_WAITING_PREFIX 一致：缺 ffmpeg/ffprobe 时任务回到队列等待。
const MEDIA_TOOLS_WAITING = '等待本机 ffmpeg/ffprobe'

function taskStatusText(task) {
  if (task.status === 'queued' && isSourceCooldown(task)) return '等待源恢复'
  if (task.status === 'queued' && String(task.error || '').startsWith(MEDIA_TOOLS_WAITING)) return '等待工具'
  if (task.status === 'failed' && task.phase === 'failed_source_exhausted') return '来源需处理'
  if (task.status !== 'failed') return statusText[task.status] || task.status
  return task.bulk_recoverable ? '失败·可重试' : '失败·需处理'
}

function isSourceCooldown(task) {
  return task.phase === 'source_cooldown' || task.phase?.startsWith('source_cooldown:')
}

// 倒计时只靠本地时钟每 30 秒重绘，不向后端发请求。
const now = ref(Date.now())
let clock = null
onMounted(() => { clock = window.setInterval(() => { now.value = Date.now() }, 30_000) })
onUnmounted(() => window.clearInterval(clock))

function taskNote(task) {
  if (task.status === 'queued' && isSourceCooldown(task)) {
    const until = untilText(task.next_retry_at, now.value)
    return `${until ? `${until}复查` : '等待调度'} · ${task.auto_retry_count || 1}/3`
  }
  if (task.status === 'failed' && task.phase === 'failed_source_exhausted') return '来源持续不可用，自动复查已停止'
  if (task.status === 'failed' && task.failure_kind === 'source_unavailable') return '来源暂不可用，可稍后重试'
  if (hasReason(task)) return failureReason(task).reason
  return task.viewkey
}

// 失败与跳过行显示一句人话原因；点状态打开建议与原始信息，失败可直接重试。
function hasReason(task) {
  return task.status === 'failed' || task.status === 'skipped'
}

async function explain(task) {
  const { reason } = failureReason(task)
  const message = failureDetail(task)
  if (task.status !== 'failed') {
    await alert({ title: reason, message })
    return
  }
  const again = await confirm({ title: reason, message, confirmText: '重试', cancelText: '关闭' })
  if (again) await retry(task.id)
}

function taskStatusTitle(task) {
  if (isSourceCooldown(task)) {
    const at = formatDateTime(task.next_retry_at)
    return [at ? `${at} 复查` : taskNote(task), task.error].filter(Boolean).join('：')
  }
  if (task.phase === 'failed_source_exhausted') {
    return [taskNote(task), task.error].filter(Boolean).join('：')
  }
  if (task.status !== 'failed') return task.error || ''
  const recovery = task.bulk_recoverable
    ? '允许有限重试，不表示来源已经恢复'
    : '需检查错误后逐项决定是否重试'
  return [recovery, task.error].filter(Boolean).join('：')
}

const selected = ref(new Set())

// 仅在 REST 列表换代时清理选择；字节级 WebSocket 更新不再重建全部 ID Set。
watch(() => props.listGeneration, () => {
  const idSet = new Set(props.tasks.map(task => task.id))
  let changed = false
  for (const id of selected.value) {
    if (!idSet.has(id)) { selected.value.delete(id); changed = true }
  }
  if (changed) emit('change', [...selected.value])
})

const allChecked = computed({
  get() {
    return props.tasks.length > 0 && selected.value.size === props.tasks.length
  },
  set(v) {
    selected.value = v ? new Set(props.tasks.map(t => t.id)) : new Set()
    emit('change', [...selected.value])
  },
})
const someChecked = computed(() =>
  selected.value.size > 0 && selected.value.size < props.tasks.length
)

function toggleOne(id) {
  if (selected.value.has(id)) selected.value.delete(id)
  else selected.value.add(id)
  selected.value = new Set(selected.value)   // 触发响应式
  emit('change', [...selected.value])
}

defineExpose({
  clearSelection: () => { selected.value = new Set(); emit('change', []) },
})

async function del(id) {
  try {
    await api.deleteTask(id)
    emit('refresh')
  } catch (e) {
    await alert({ title: '无法删除', message: apiErrorText(e) })
  }
}
async function retry(id) {
  try {
    await api.retryTask(id)
    emit('refresh')
  } catch (e) {
    await alert({ title: '无法重试', message: apiErrorText(e) })
  }
}

const phaseText = {
  resolving: '解析下载地址',
  waiting: '等待数据',
  network_wait: '网络中断，等待恢复',
  processing: '封装校验中',
}

function finiteNumber(value) {
  const number = Number(value)
  return Number.isFinite(number) ? number : null
}

function legacySpeed(t) {
  return typeof t.speed === 'string' ? t.speed.trim() : ''
}

function runningTelemetry(t) {
  const speed = formatSpeed(t.speed_bps)
  if (t.phase === 'downloading') return speed || legacySpeed(t) || '下载中'
  if (phaseText[t.phase]) return phaseText[t.phase]
  if (t.phase === 'retrying') {
    const attempt = Math.max(1, Math.min(3, Math.round(finiteNumber(t.attempt) || 1)))
    return `重试 ${attempt}/3`
  }
  return speed || legacySpeed(t) || '准备中'
}

function taskEta(t) {
  if (t.status !== 'running' || t.phase !== 'downloading') return ''
  return etaText(t.downloaded_bytes, t.total_bytes, t.speed_bps)
}

function telemetryClass(t) {
  if (formatSpeed(t.speed_bps) || (t.phase === 'downloading' && legacySpeed(t))) return 'speed'
  if (t.phase === 'waiting' || t.phase === 'network_wait') return 'waiting'
  if (t.phase === 'retrying') return 'retrying'
  if (t.phase === 'processing') return 'processing'
  return 'phase'
}

function progressValue(t) {
  if (t.status === 'completed') return 100
  const downloaded = finiteNumber(t.downloaded_bytes)
  const totalBytes = finiteNumber(t.total_bytes)
  if (downloaded !== null && totalBytes !== null && totalBytes > 0) {
    return Math.min(100, Math.max(0, downloaded / totalBytes * 100))
  }
  const progress = finiteNumber(t.progress)
  if (progress !== null) return Math.min(100, Math.max(0, progress))
  return 0
}

function hasDeterminateProgress(t) {
  const totalBytes = finiteNumber(t.total_bytes)
  if (totalBytes !== null && totalBytes > 0) return true
  if (t.status === 'completed') return true
  const progress = finiteNumber(t.progress)
  return progress !== null && (progress > 0 || t.status !== 'running')
}

function isIndeterminate(t) {
  return t.status === 'running' && !hasDeterminateProgress(t)
}

function progressText(t) {
  return isIndeterminate(t) ? '…' : `${Math.round(progressValue(t))}%`
}

function byteSummary(t) {
  const downloaded = finiteNumber(t.downloaded_bytes)
  const totalBytes = finiteNumber(t.total_bytes)
  if (downloaded !== null && totalBytes !== null && totalBytes > 0) {
    return `${formatBytes(downloaded)} / ${formatBytes(totalBytes)}`
  }
  return downloaded !== null && downloaded > 0 ? `已下载 ${formatBytes(downloaded)}` : ''
}

function formatProgressTime(value) {
  const stamp = progressTime(value)
  return formatDateTime(Number.isFinite(stamp) ? new Date(stamp).toISOString() : value, String(value || ''))
}

function telemetryTitle(t) {
  const eta = taskEta(t)
  const details = [
    t.status === 'running' ? runningTelemetry(t) : '',
    eta ? `预计剩余${eta}` : '',
    byteSummary(t),
  ]
  const lastProgress = formatProgressTime(t.last_progress_at)
  if (lastProgress) details.push(`最后进度 ${lastProgress}`)
  return details.filter(Boolean).join(' · ')
}

function progressAriaText(t) {
  const progress = isIndeterminate(t) ? '任务进度未知' : `任务进度 ${Math.round(progressValue(t))}%`
  const details = [progress]
  if (t.status === 'running') details.push(runningTelemetry(t))
  const bytes = byteSummary(t)
  if (bytes) details.push(bytes)
  return details.join('，')
}

// ---------- 虚拟滚动 ----------
// 任务可达数千条，全量渲染会让每条 WebSocket 进度更新都在巨大的虚拟 DOM 上做 diff，
// 越用越卡。这里只渲染视口内 ±缓冲的行，其余用 spacer 撑出滚动高度。
const ROW_H = 62           // 行高（与 .task-row 固定高度一致）
const BUFFER = 6           // 视口上下各多渲染几行，滚动时不露白
const viewport = ref(null)
const scrollTop = ref(0)
const viewH = ref(600)

let ro = null
function measure() {
  if (viewport.value) viewH.value = viewport.value.clientHeight || 600
}
function observeViewport(element) {
  ro?.disconnect()
  ro = null
  if (!element) return
  measure()
  if (window.ResizeObserver) {
    ro = new ResizeObserver(measure)
    ro.observe(element)
  }
}
function onScroll() {
  if (viewport.value) scrollTop.value = viewport.value.scrollTop
}

onMounted(() => {
  if (!window.ResizeObserver) {
    window.addEventListener('resize', measure)
  }
})
onUnmounted(() => {
  if (ro) ro.disconnect()
  window.removeEventListener('resize', measure)
})
watch(viewport, observeViewport, { flush: 'post' })

// 列表大幅变化（切换/刷新）后，若当前滚动位置已越界则归位
watch(() => props.tasks.length, async (n) => {
  await nextTick()
  if (viewport.value && viewport.value.scrollTop > n * ROW_H) {
    viewport.value.scrollTop = 0
    scrollTop.value = 0
  }
})

const total = computed(() => props.tasks.length)
const startIndex = computed(() =>
  Math.max(0, Math.floor(scrollTop.value / ROW_H) - BUFFER)
)
const visibleCount = computed(() =>
  Math.ceil(viewH.value / ROW_H) + BUFFER * 2
)
const endIndex = computed(() =>
  Math.min(total.value, startIndex.value + visibleCount.value)
)
const visibleTasks = computed(() =>
  props.tasks.slice(startIndex.value, endIndex.value)
)
const offsetY = computed(() => startIndex.value * ROW_H)
const spacerH = computed(() => total.value * ROW_H)
</script>

<template>
  <div v-if="loading && tasks.length === 0" class="empty">正在加载任务…</div>

  <div v-else-if="tasks.length === 0" class="empty" :class="emptyTone">
    {{ emptyText }}
    <small v-if="emptyHint" class="empty-hint">{{ emptyHint }}</small>
  </div>

  <div v-else class="tasks" role="table" aria-label="任务列表">
    <div class="tasks-head" role="row">
      <div role="columnheader">
        <label class="checkbox-hit">
          <input type="checkbox"
                 :checked="allChecked"
                 :indeterminate.prop="someChecked"
                 aria-label="全选/取消全选"
                 @change="allChecked = $event.target.checked" />
        </label>
      </div>
      <div role="columnheader">标题</div>
      <div role="columnheader">模特</div>
      <div role="columnheader">状态</div>
      <div role="columnheader">进度</div>
      <div role="columnheader"><span class="sr-only">操作</span></div>
    </div>

    <div ref="viewport" class="tasks-viewport" @scroll.passive="onScroll">
      <div class="tasks-spacer" :style="{ height: spacerH + 'px' }">
        <div class="tasks-window" :style="{ transform: `translateY(${offsetY}px)` }">
          <div v-for="t in visibleTasks" :key="t.id" class="task-row" role="row"
               :class="{ selected: selected.has(t.id) }">
            <div role="cell">
              <label class="checkbox-hit">
                <input type="checkbox"
                       :checked="selected.has(t.id)"
                       :aria-label="`选择任务 ${t.title}`"
                       @change="toggleOne(t.id)" />
              </label>
            </div>
            <div class="col-title" role="cell">
              <div class="t" :title="t.title">{{ t.title }}</div>
              <div class="title-meta">
                <span class="vk" :class="{ reason: hasReason(t) }" :title="t.viewkey">{{ taskNote(t) }}</span>
                <span v-if="t.status === 'running'"
                      class="mobile-task-telemetry"
                      :class="telemetryClass(t)"
                      :title="telemetryTitle(t)"
                      :aria-label="`任务状态：${runningTelemetry(t)}`">
                  {{ runningTelemetry(t) }}
                </span>
              </div>
            </div>
            <div class="col-model" role="cell" :title="t.model_name">{{ t.model_name || '—' }}</div>
            <div role="cell">
              <button v-if="hasReason(t)" type="button" class="status-hit"
                      :title="`${failureReason(t).reason}；点击查看建议`"
                      :aria-label="`${taskStatusText(t)}：${failureReason(t).reason}。查看原因与建议`"
                      @click="explain(t)">
                <span class="status-pill" :class="t.status">{{ taskStatusText(t) }}</span>
              </button>
              <span v-else class="status-pill" :class="t.status"
                    :title="taskStatusTitle(t)">{{ taskStatusText(t) }}</span>
            </div>
            <div class="progress-block" role="cell">
              <div class="progress-bar"
                   :class="[t.status, `phase-${t.phase || 'unknown'}`, { indeterminate: isIndeterminate(t) }]"
                   role="progressbar"
                   :aria-label="`${t.title} 任务进度`"
                   :aria-valuenow="isIndeterminate(t) ? undefined : Math.round(progressValue(t))"
                   :aria-valuetext="progressAriaText(t)"
                   aria-valuemin="0" aria-valuemax="100">
                <div :style="{ '--progress': progressValue(t) / 100 }"></div>
              </div>
              <span class="progress-pct" :title="byteSummary(t)">{{ progressText(t) }}</span>
              <span v-if="t.status === 'running'"
                    class="progress-telemetry"
                    :class="telemetryClass(t)"
                    :title="telemetryTitle(t)">
                {{ runningTelemetry(t) }}<span v-if="taskEta(t)" class="telemetry-eta"> · {{ taskEta(t) }}</span>
              </span>
            </div>
            <div style="text-align:right" role="cell">
              <button v-if="t.status === 'failed' || t.status === 'skipped'"
                      class="btn ghost sm" @click="retry(t.id)" title="重新入队">重试</button>
              <button class="btn danger sm"
                      :disabled="t.task_type === 'upload' && t.status === 'running'"
                      @click="del(t.id)"
                      :title="t.task_type === 'upload' && t.status === 'running'
                        ? '上传进行中，完成后才能删除' : '删除任务'">删除</button>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>
