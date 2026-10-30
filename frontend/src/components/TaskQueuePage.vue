<script setup>
import { computed, onMounted, ref } from 'vue'
import api from '../api'
import TaskTable from './TaskTable.vue'
import DownloadSettingsPanel from './DownloadSettingsPanel.vue'
import { useTaskQueue } from '../composables/useTaskQueue'
import { alert, confirm } from '../composables/useDialog'
import { apiErrorText } from '../utils/apiError'
import { formatBytes, formatDuration } from '../utils/formatters'

const QUEUE_CONFIG = {
  download: {
    title: '下载',
    subtitle: '并发 5 · yt-dlp 原生 HLS 下载',
    verb: '下载',
    retryLabel: '重试可重试项',
    retryCountKey: 'failed_recoverable',
    stats: [
      ['active', '活动'],
      ['running', '进行中'],
      ['queued', '排队'],
      ['paused', '暂停'],
      ['completed', '完成'],
      ['failed', '失败'],
      ['failed_recoverable', '可重试'],
      ['failed_manual', '需处理'],
      ['all', '全部'],
    ],
    hint: '来源暂不可用会有限延后复查；可重试表示允许再次尝试，不保证来源已恢复。',
    pauseTitle: '',
    selectedMessage: count => `删除已选 ${count} 个下载任务？`,
    allMessage: (count, scope = '') => `删除全部 ${count} 个下载任务${scope}？正在运行的会被取消。`,
  },
  upload: {
    title: '上传',
    subtitle: '并发 2 · 上传前二次去重 · 优先秒传',
    verb: '上传',
    retryLabel: '重试失败',
    retryCountKey: 'failed',
    stats: [
      ['active', '活动'],
      ['running', '进行中'],
      ['queued', '排队'],
      ['paused', '暂停'],
      ['completed', '完成'],
      ['skipped', '跳过'],
      ['failed', '失败'],
      ['all', '全部'],
    ],
    hint: '下载完成自动入队，上传前二次去重，115 已有则跳过',
    pauseTitle: '暂停排队任务；已经开始的上传会安全完成',
    selectedMessage: count => `删除已选 ${count} 个上传任务？`,
    allMessage: (count, scope = '') => `删除全部 ${count} 个上传任务${scope}？运行中的上传不会被中断，将保留到完成。`,
  },
}

const props = defineProps({
  taskType: {
    type: String,
    required: true,
    validator: value => value === 'download' || value === 'upload',
  },
})
const config = QUEUE_CONFIG[props.taskType]
const {
  tasks,
  loading,
  refreshing,
  loadingMore,
  loadError,
  downloadBlocked,
  uploadBlocked,
  network,
  total,
  matched,
  counts,
  selectedIds,
  tableRef,
  nextCursor,
  listGeneration,
  view,
  search,
  today,
  recentCompletions,
  completedBump,
  setView,
  setSearch,
  load,
  loadMore,
  startAll,
  pauseAll,
  resumeAll,
  retryFailed,
  deleteSelected,
  deleteAll,
} = useTaskQueue(props.taskType)

const operation = ref('')
// 下载运行参数：副标题显示实际并发/限速/时段，面板内可调整。
const downloadSettings = ref(null)
const settingsOpen = ref(false)

const subtitle = computed(() => {
  const values = downloadSettings.value
  if (props.taskType !== 'download' || !values) return config.subtitle
  const parts = [`并发 ${values.effective_concurrency}`, 'yt-dlp 原生 HLS 下载']
  if (Number(values.rate_limit_mbps) > 0) parts.push(`限速约 ${values.rate_limit_mbps} MB/s`)
  if (values.window_text) parts.push(`时段 ${values.window_text}`)
  return parts.join(' · ')
})

async function loadDownloadSettings() {
  if (props.taskType !== 'download') return
  try {
    const { data } = await api.downloadSettings()
    downloadSettings.value = data
  } catch (_) { /* 旧后端没有该接口时保留原副标题 */ }
}

function onSettingsSaved(data) {
  downloadSettings.value = data
  settingsOpen.value = false
  void load()
}

onMounted(loadDownloadSettings)

// 门禁原因决定提示的语气：时段是用户主动设置，不按错误显示。
const blockedTail = computed(() => ({
  network_outage: '链路恢复后自动继续，断流期间的失败不计入重试次数',
  rate_limited: '冷却结束后自动继续，被限流的任务不计入重试次数',
  download_window: '到时段开始后自动继续，正在下载的任务会下完',
}[downloadBlocked.value?.code] || '工具可用后会自动继续'))
const busy = computed(() => Boolean(operation.value))
const filtered = computed(() => view.value !== 'all' || Boolean(search.value.trim()))

function chipCount(key) {
  const value = name => Number(counts.value[name] || 0)
  if (key === 'active') return value('running') + value('queued') + value('paused') + value('failed')
  if (key === 'all') return total.value
  return value(key)
}

// “暂停”只在确有暂停任务或正在查看时出现，平时不占位置。
const statChips = computed(() => config.stats.filter(([key]) => (
  key !== 'paused' || chipCount('paused') > 0 || view.value === 'paused')))

function chipTitle(key, label) {
  if (key === 'active') return '进行中、排队、暂停与失败的任务'
  if (key === 'all') return '包括已完成与跳过的全部任务'
  return `只看${label}`
}

// 近一小时反复断流时提示；正在断流时由“新下载已暂缓”说明，不重复显示。
const networkHint = computed(() => {
  const health = network.value
  if (!health || health.outage || Number(health.recent_outages || 0) < 2) return ''
  const total = formatDuration(health.recent_outage_seconds) || '不到 1 分钟'
  return `近 1 小时网络断流 ${health.recent_outages} 次，累计${total}。`
    + '断流期间不计入重试次数，恢复后自动继续；若一直如此，建议换一个代理节点。'
})

const queueCleared = computed(() => view.value === 'active' && !search.value.trim())

const emptyText = computed(() => {
  const keyword = search.value.trim()
  if (keyword) return `没有匹配“${keyword}”的任务`
  if (queueCleared.value) return '队列已清空'
  return view.value === 'all' ? '暂无任务' : '当前筛选下没有任务'
})

const clockFormatter = new Intl.DateTimeFormat('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false })

// 队列清空时给出今天的成绩；没有完成记录时只提示可以看全部历史。
const emptyHint = computed(() => {
  if (!queueCleared.value) return ''
  const summary = today.value
  if (!summary?.completed) return '今天还没有完成的任务，点“全部”可查看历史记录'
  const parts = [`今天完成 ${summary.completed} 个`]
  if (summary.bytes > 0) parts.push(formatBytes(summary.bytes))
  const last = summary.last_completed_at ? new Date(summary.last_completed_at) : null
  if (last && !Number.isNaN(last.getTime())) parts.push(`最后一个 ${clockFormatter.format(last)}`)
  return parts.join(' · ')
})
const recoverableCount = computed(() => Number(counts.value[config.retryCountKey] || 0))
const retryButtonTitle = computed(() => {
  if (recoverableCount.value) {
    return props.taskType === 'download'
      ? '先校验已有成品，再有限重试；来源持续不可用时会延后复查'
      : '重新入队失败的上传任务'
  }
  return counts.value.failed
    ? `当前 ${counts.value.failed} 个失败均需逐项检查`
    : '当前没有失败任务'
})

async function execute(label, action) {
  if (busy.value) return { ok: false }
  operation.value = label
  try {
    return { ok: true, data: await action() }
  } catch (error) {
    await alert({ title: `${label}失败`, message: apiErrorText(error) })
    return { ok: false }
  } finally {
    operation.value = ''
  }
}

async function removeSelected() {
  if (!selectedIds.value.length) return
  const accepted = await confirm({
    title: '删除已选任务',
    message: config.selectedMessage(selectedIds.value.length),
    danger: true,
    confirmText: '删除',
  })
  if (!accepted) return
  const result = await execute('删除', deleteSelected)
  await showRetainedUploads(result, '部分任务已保留')
}

async function removeAll() {
  if (!total.value) return
  const accepted = await confirm({
    title: '删除全部任务',
    message: config.allMessage(total.value, filtered.value ? '（包括当前筛选之外的任务）' : ''),
    danger: true,
    confirmText: '全部删除',
  })
  if (!accepted) return
  const result = await execute('删除', deleteAll)
  await showRetainedUploads(result, '运行中上传已保留')
}

async function showRetainedUploads(result, title) {
  const retained = result.data?.skipped_running_uploads || 0
  if (!result.ok || !retained) return
  await alert({
    title,
    message: `${retained} 个上传正在由 115 客户端处理，完成后才能删除。`,
  })
}

async function requestMore() {
  await execute('加载更多', loadMore)
}

async function retryRecoverable() {
  const result = await execute(config.retryLabel, retryFailed)
  if (!result.ok) return
  const retried = result.data?.retried || 0
  const reconciled = result.data?.reconciled || 0
  const skipped = result.data?.skipped_non_retryable || 0
  if (!reconciled && !skipped) return
  const summary = []
  if (reconciled) summary.push(`${reconciled} 个已有成品校验通过并补记完成`)
  if (retried) summary.push(`${retried} 个任务已按安全策略重新入队`)
  if (skipped) summary.push(`${skipped} 个确定性失败保留供逐项处理`)
  await alert({
    title: '恢复结果',
    message: `${summary.join('；')}。`,
  })
}
</script>

<template>
  <section :aria-labelledby="`${taskType}-queue-title`"
           :aria-busy="loading || refreshing || busy">
    <div class="page-title">
      <h2 :id="`${taskType}-queue-title`">{{ config.title }}</h2>
      <div class="sub">
        {{ subtitle }}
        <button v-if="taskType === 'download' && downloadSettings" type="button"
                class="btn ghost sm settings-toggle" :aria-expanded="settingsOpen"
                aria-controls="download-settings" @click="settingsOpen = !settingsOpen">
          {{ settingsOpen ? '收起设置' : '下载设置' }}
        </button>
      </div>
    </div>

    <DownloadSettingsPanel v-if="settingsOpen && downloadSettings" id="download-settings"
                           :settings="downloadSettings" @saved="onSettingsSaved"
                           @close="settingsOpen = false" />

    <div class="stat-strip" role="group" :aria-label="`${config.title}任务筛选与统计`">
      <button v-for="([key, label]) in statChips" :key="key" type="button"
              :class="['stat-chip', key, { selected: view === key }]"
              :aria-pressed="view === key" :title="chipTitle(key, label)"
              @click="setView(key)">
        <b :key="key === 'completed' ? `completed-${completedBump}` : key"
           :class="{ bump: key === 'completed' && completedBump > 0 }">{{ loading ? '…' : chipCount(key) }}</b>
        {{ label }}
      </button>
    </div>
    <TransitionGroup tag="ul" name="done-toast" class="done-ticker" aria-live="polite"
                     :aria-label="`刚完成的${config.title}任务`">
      <li v-for="item in recentCompletions" :key="item.id">
        <span class="done-mark" aria-hidden="true">✓</span>
        已完成 · <span class="done-title">{{ item.title }}</span>
      </li>
    </TransitionGroup>

    <div class="toolbar" :aria-label="`${config.title}任务操作`">
      <button class="btn" :disabled="busy" @click="execute(`全部${config.verb}`, startAll)">
        全部{{ config.verb }}
      </button>
      <button class="btn secondary" :disabled="busy" :title="config.pauseTitle"
              @click="execute('全部停止', pauseAll)">全部停止</button>
      <button class="btn secondary" :disabled="busy"
              @click="execute('全部继续', resumeAll)">全部继续</button>
      <button class="btn secondary sm" :disabled="busy || !recoverableCount"
              :title="retryButtonTitle"
              @click="retryRecoverable">
        {{ config.retryLabel }}{{ recoverableCount ? ` (${recoverableCount})` : '' }}
      </button>
      <button class="btn danger sm" :disabled="busy || !selectedIds.length"
              @click="removeSelected">
        删除已选{{ selectedIds.length ? ` (${selectedIds.length})` : '' }}
      </button>
      <button class="btn danger sm" :disabled="busy || !total" @click="removeAll">
        删除全部
      </button>
      <span class="spacer"></span>
      <input :value="search" type="search" class="queue-search" maxlength="100"
             placeholder="搜索标题、模特或 viewkey" :aria-label="`搜索${config.title}任务`"
             @input="setSearch($event.target.value)" @keydown.esc="setSearch('')" />
      <button class="btn ghost sm" :disabled="loading || refreshing || busy" @click="load">
        {{ loading || refreshing ? '加载中…' : '刷新' }}
      </button>
    </div>
    <p v-if="config.hint" class="hint queue-hint">{{ config.hint }}</p>
    <p v-if="networkHint" class="hint queue-hint network-hint" role="status">{{ networkHint }}</p>

    <div v-if="loadError" class="queue-error" role="alert">{{ loadError }}</div>
    <div v-if="downloadBlocked" role="status"
         :class="downloadBlocked.code === 'download_window' ? 'queue-note' : 'queue-error'">
      新下载已暂缓：{{ downloadBlocked.message }}。排队任务保持原状，{{ blockedTail }}。
    </div>
    <div v-if="uploadBlocked" class="queue-error" role="status">
      上传已暂停：{{ uploadBlocked.message }}。排队任务保持原状且不消耗重试次数，程序每 10 分钟自动复查，恢复后自动继续。
    </div>
    <TaskTable ref="tableRef" :tasks="tasks" :loading="loading"
               :list-generation="listGeneration" :empty-text="emptyText"
               :empty-hint="emptyHint" :empty-tone="queueCleared ? 'cleared' : ''"
               @change="ids => selectedIds = ids" @refresh="load" />
    <div v-if="matched" class="pagination-bar" aria-label="任务分页" role="status">
      <span>已显示 {{ tasks.length }} / {{ matched }}<template v-if="filtered"> · 共 {{ total }}</template></span>
      <button v-if="nextCursor && tasks.length < matched" class="btn ghost sm"
              :disabled="loadingMore || busy" @click="requestMore">
        {{ loadingMore ? '加载中…' : '加载更多' }}
      </button>
    </div>
  </section>
</template>
