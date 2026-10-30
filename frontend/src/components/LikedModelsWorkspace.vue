<script setup>
import { useRealtimeRefresh } from '../composables/useRealtimeRefresh'
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import api, { subscribeWS } from '../api'
import { confirm } from '../composables/useDialog'
import { apiErrorText } from '../utils/apiError'
import { runModelSyncJob } from '../utils/modelSyncJob'
import LikedModelCard from './LikedModelCard.vue'
import VideoListModal from './VideoListModal.vue'

const payload = ref(null)
const loading = ref(true)
const refreshing = ref(false)
const error = ref('')
const notice = ref('')
const search = ref('')
const sortBy = ref('liked_desc')
const visibleLimit = ref(24)
const selected = ref(new Set())
const syncing = ref(new Set())
const syncProgress = ref(null)
const queueProgress = ref(null)
const detailModel = ref(null)
const workspaceRef = ref(null)

let unsubscribeWS = null
let refreshTimer = null
let visibilityObserver = null
let mounted = false
let loadEnabled = false
let loadDirty = true
let loadPromise = null
let loadController = null
let loadGeneration = 0
let syncJobController = null

const models = computed(() => payload.value?.models || [])
const summary = computed(() => payload.value?.summary || {
  liked_models: 0,
  synced_models: 0,
  unsynced_models: 0,
  bound_models: 0,
  total_videos: 0,
  cloud_videos: 0,
  maybe_videos: 0,
  missing_videos: 0,
  downloadable_videos: 0,
  pending_upload_videos: 0,
})
const actionBusy = computed(() => Boolean(syncProgress.value || queueProgress.value))
const normalizedSearch = computed(() => search.value.trim().toLocaleLowerCase())

const filteredModels = computed(() => {
  const query = normalizedSearch.value
  const items = query
    ? models.value.filter(model => [model.name, model.ph_id, model.profile_slug]
      .some(value => String(value || '').toLocaleLowerCase().includes(query)))
    : [...models.value]
  return items.sort((left, right) => {
    if (sortBy.value === 'name') {
      return String(left.name || '').localeCompare(String(right.name || ''), 'zh-CN')
    }
    if (sortBy.value === 'missing_desc') {
      return Number(right.downloadable_videos || 0) - Number(left.downloadable_videos || 0)
        || String(left.name || '').localeCompare(String(right.name || ''), 'zh-CN')
    }
    if (sortBy.value === 'unsynced_first') {
      return Number(Boolean(left.synced_at)) - Number(Boolean(right.synced_at))
        || String(left.name || '').localeCompare(String(right.name || ''), 'zh-CN')
    }
    return String(right.liked_at || '').localeCompare(String(left.liked_at || ''))
      || Number(right.feedback_id || 0) - Number(left.feedback_id || 0)
  })
})

const visibleModels = computed(() => filteredModels.value.slice(0, visibleLimit.value))
const selectedModels = computed(() => models.value.filter(model => selected.value.has(model.id)))
const selectedDownloadable = computed(() => selectedModels.value.filter(model => (
  Boolean(model.synced_at) && Number(model.downloadable_videos || 0) > 0
)))
const selectedDownloadCount = computed(() => selectedDownloadable.value.reduce(
  (total, model) => total + Number(model.downloadable_videos || 0), 0))
const selectedUnsyncedCount = computed(() => selectedModels.value.filter(
  model => !model.synced_at).length)
const selectedWithoutMissingCount = computed(() => selectedModels.value.filter(
  model => model.synced_at && Number(model.downloadable_videos || 0) === 0).length)
const allFilteredSelected = computed(() => (
  filteredModels.value.length > 0
  && filteredModels.value.every(model => selected.value.has(model.id))
))
const unsyncedModels = computed(() => models.value.filter(model => !model.synced_at))

watch([search, sortBy], () => { visibleLimit.value = 24 })

function pruneSelection(items) {
  const available = new Set(items.map(model => model.id))
  selected.value = new Set([...selected.value].filter(id => available.has(id)))
}

async function load(silent = false) {
  loadDirty = true
  if (!mounted || !loadEnabled) return false
  if (loadPromise) return loadPromise
  loadPromise = (async () => {
    let loaded = false
    while (loadDirty && mounted && loadEnabled) {
      loadDirty = false
      const generation = ++loadGeneration
      const controller = new AbortController()
      loadController = controller
      if (silent && payload.value) refreshing.value = true
      else loading.value = true
      try {
        const { data } = await api.tasteLikedModels({ signal: controller.signal })
        if (generation !== loadGeneration || !mounted) continue
        payload.value = data
        pruneSelection(data.models || [])
        error.value = ''
        loaded = true
      } catch (requestError) {
        if (requestError.code !== 'ERR_CANCELED' && generation === loadGeneration) {
          error.value = `加载“我的符合”失败：${apiErrorText(requestError)}`
        }
      } finally {
        if (generation === loadGeneration) {
          loading.value = false
          refreshing.value = false
        }
      }
    }
    return loaded
  })()
  try {
    return await loadPromise
  } finally {
    loadPromise = null
    loadController = null
    if (loadDirty && mounted && loadEnabled) void load(true)
  }
}

function toggleSelection(modelId, checked) {
  const next = new Set(selected.value)
  if (checked) next.add(modelId)
  else next.delete(modelId)
  selected.value = next
}

function toggleFilteredSelection() {
  const next = new Set(selected.value)
  if (allFilteredSelected.value) {
    for (const model of filteredModels.value) next.delete(model.id)
  } else {
    for (const model of filteredModels.value) next.add(model.id)
  }
  selected.value = next
}

function clearSelection() {
  selected.value = new Set()
}

function markSyncing(ids, active) {
  const next = new Set(syncing.value)
  for (const id of ids) active ? next.add(id) : next.delete(id)
  syncing.value = next
}

async function syncOne(modelId) {
  if (actionBusy.value || syncing.value.has(modelId)) return
  const model = models.value.find(item => item.id === modelId)
  markSyncing([modelId], true)
  notice.value = ''
  error.value = ''
  try {
    await api.syncModel(modelId)
    await load(true)
    notice.value = `${model?.name || '该模特'} 的作品与 115 库存已校准；没有创建下载任务。`
  } catch (requestError) {
    error.value = `校准库存失败：${apiErrorText(requestError)}`
  } finally {
    markSyncing([modelId], false)
  }
}

async function syncMany(items, label) {
  if (!items.length || actionBusy.value) return
  const accepted = await confirm({
    title: `校准 ${items.length} 位模特的库存`,
    message: `将联网读取 ${items.length} 位模特的作品列表，并与 115 目录逐一比对。这个过程只更新本地快照，不会下载视频或上传文件；耗时取决于作品数量。`,
    confirmText: '开始校准',
  })
  if (!accepted) return
  if (!mounted) return

  const ids = items.map(model => model.id)
  markSyncing(ids, true)
  syncProgress.value = { done: 0, total: ids.length, label }
  notice.value = ''
  error.value = ''
  const controller = new AbortController()
  syncJobController?.abort()
  syncJobController = controller
  try {
    const result = await runModelSyncJob(ids, {
      onProgress: job => {
        syncProgress.value = {
          done: Number(job.done || 0),
          total: Number(job.total || ids.length),
          label,
        }
      },
      onRefresh: () => load(true),
      shouldPoll: () => mounted,
      signal: controller.signal,
      onConnectionState: state => {
        if (state.state === 'retrying') {
          notice.value = `状态连接暂时中断，正在自动重试（${state.attempt}）…`
        }
      },
    })
    notice.value = result.failed.length
      ? `${result.completed} 位校准完成，${result.failed.length} 位失败；失败项未创建任何媒体任务。`
      : `${result.completed} 位库存校准完成；现在可以审核明确缺失数量。`
  } catch (requestError) {
    if (requestError.name !== 'AbortError' && requestError.code !== 'ERR_CANCELED') {
      error.value = `批量校准失败：${apiErrorText(requestError)}`
    }
  } finally {
    if (syncJobController === controller) syncJobController = null
    markSyncing(ids, false)
    syncProgress.value = null
  }
}

function syncUnsynced() {
  return syncMany(unsyncedModels.value, '正在校准待处理模特')
}

function syncSelected() {
  return syncMany(selectedModels.value, '正在重新校准所选模特')
}

function downloadConfirmationMessage(items, count) {
  const skipped = []
  if (selectedUnsyncedCount.value) skipped.push(`${selectedUnsyncedCount.value} 位尚未校准`)
  if (selectedWithoutMissingCount.value) skipped.push(`${selectedWithoutMissingCount.value} 位当前无明确缺失`)
  const skippedText = skipped.length ? `；另有 ${skipped.join('、')}，本次会跳过` : ''
  return `将为 ${items.length} 位模特的 ${count} 个“明确缺失”视频创建下载任务${skippedText}。疑似已有不会加入；每个视频下载完成后会按现有规则自动创建上传任务，并在上传前再次与 115 去重。`
}

async function queueModels(items, { useSelectionContext = false } = {}) {
  const eligible = items.filter(model => (
    Boolean(model.synced_at) && Number(model.downloadable_videos || 0) > 0
  ))
  const count = eligible.reduce(
    (total, model) => total + Number(model.downloadable_videos || 0), 0)
  if (!eligible.length || !count || actionBusy.value) return

  const accepted = await confirm({
    title: `确认下载 ${count} 个视频`,
    message: useSelectionContext
      ? downloadConfirmationMessage(eligible, count)
      : `将为“${eligible[0].name}”创建 ${count} 个明确缺失视频的下载任务。疑似已有不会加入；下载完成后会按现有规则自动创建上传任务，并在上传前再次与 115 去重。`,
    confirmText: '加入下载队列',
  })
  if (!accepted) return
  if (!mounted) return

  const ids = eligible.map(model => model.id)
  queueProgress.value = { done: 0, total: eligible.length, added: 0 }
  notice.value = ''
  error.value = ''
  let added = 0
  const failures = []
  try {
    for (let index = 0; index < eligible.length; index += 1) {
      const model = eligible[index]
      try {
        const { data } = await api.addDownload(model.id, 'missing')
        added += Number(data.added || 0)
      } catch (requestError) {
        failures.push(`${model.name}：${apiErrorText(requestError)}`)
      }
      queueProgress.value = {
        done: index + 1,
        total: eligible.length,
        added,
      }
    }
    await load(true)
    if (failures.length) {
      error.value = `已加入 ${added} 个任务；${failures.length} 位处理失败：${failures.slice(0, 3).join('；')}`
    } else {
      notice.value = added
        ? `已加入 ${added} 个下载任务；完成后会自动进入上传队列。`
        : '没有新增任务：这些视频可能已在下载或上传流水线中。'
    }
  } finally {
    queueProgress.value = null
    markSyncing(ids, false)
  }
}

function queueOne(model) {
  return queueModels([model])
}

function queueSelected() {
  return queueModels(selectedModels.value, { useSelectionContext: true })
}

function openDetail(model) {
  if (!model.synced_at || actionBusy.value) return
  detailModel.value = model
}

async function onManualQueued(added) {
  notice.value = Number(added || 0)
    ? `已加入 ${added} 个手选下载任务；完成后会自动进入上传队列。`
    : '没有新增任务。'
  await load(true)
}

function exportRows() {
  return models.value.map(model => ({
    id: model.id,
    name: model.name,
    profile_url: model.url,
    liked_at: model.liked_at,
    feedback_run_id: model.feedback_run_id,
    synced_at: model.synced_at,
    cloud_bound: Boolean(model.cloud_bound),
    cloud_folder_name: model.cloud_folder?.name || '',
    cloud_folder_cid: model.cloud_folder?.cid || '',
    total_videos: Number(model.total_videos || 0),
    cloud_videos: Number(model.cloud_videos || 0),
    maybe_videos: Number(model.maybe_videos || 0),
    missing_videos: Number(model.missing_videos || 0),
    downloadable_videos: Number(model.downloadable_videos || 0),
    pending_upload_videos: Number(model.pending_upload_videos || 0),
  }))
}

function saveFile(filename, content, type) {
  const blob = new Blob([content], { type })
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
  window.setTimeout(() => URL.revokeObjectURL(url), 1000)
}

function exportJson() {
  const document = {
    schema: 'ph115-liked-models-v1',
    exported_at: new Date().toISOString(),
    summary: summary.value,
    models: exportRows(),
  }
  saveFile(
    `ph115-liked-models-${new Date().toISOString().slice(0, 10)}.json`,
    JSON.stringify(document, null, 2),
    'application/json;charset=utf-8',
  )
  notice.value = `已导出 ${models.value.length} 位“符合”模特的 JSON 名单。`
}

function csvCell(value) {
  return `"${String(value ?? '').replaceAll('"', '""')}"`
}

function exportCsv() {
  const rows = exportRows()
  const fields = Object.keys(rows[0] || {
    id: '', name: '', profile_url: '', liked_at: '',
  })
  const csv = [
    fields.map(csvCell).join(','),
    ...rows.map(row => fields.map(field => csvCell(row[field])).join(',')),
  ].join('\r\n')
  saveFile(
    `ph115-liked-models-${new Date().toISOString().slice(0, 10)}.csv`,
    `\ufeff${csv}`,
    'text/csv;charset=utf-8',
  )
  notice.value = `已导出 ${models.value.length} 位“符合”模特的 CSV 名单。`
}

function scheduleRefresh() {
  loadDirty = true
  if (!loadEnabled) return
  if (refreshTimer) window.clearTimeout(refreshTimer)
  refreshTimer = window.setTimeout(() => {
    refreshTimer = null
    void load(true)
  }, 1200)
}

function onWSMessage(message) {
  if (message.type === 'model_update' && message.model && payload.value) {
    const index = models.value.findIndex(model => model.id === message.model.id)
    if (index < 0) return
    const previous = models.value[index]
    const updated = { ...previous, ...message.model }
    const nextModels = [...models.value]
    nextModels[index] = updated
    const countFields = [
      'total_videos', 'cloud_videos', 'maybe_videos', 'missing_videos',
      'downloadable_videos', 'pending_upload_videos',
    ]
    const nextSummary = { ...summary.value }
    for (const field of countFields) {
      nextSummary[field] = Math.max(0,
        Number(nextSummary[field] || 0)
        + Number(updated[field] || 0) - Number(previous[field] || 0))
    }
    payload.value = { ...payload.value, models: nextModels, summary: nextSummary }
    if (loadPromise) loadDirty = true
    return
  }
  if (message.type === 'library_membership_changed') {
    scheduleRefresh()
    return
  }
  if (message.type === 'task_update'
      && message.task?.status
      && ['queued', 'completed', 'failed', 'skipped'].includes(message.task.status)) {
    scheduleRefresh()
  }
}

function enableLoading() {
  if (loadEnabled || !mounted) return
  loadEnabled = true
  visibilityObserver?.disconnect()
  visibilityObserver = null
  unsubscribeWS ||= subscribeWS(onWSMessage)
  void load()
}

onMounted(() => {
  mounted = true
  if (!window.IntersectionObserver) {
    enableLoading()
    return
  }
  visibilityObserver = new IntersectionObserver(entries => {
    if (entries.some(entry => entry.isIntersecting)) enableLoading()
  }, { rootMargin: '400px 0px' })
  if (workspaceRef.value) visibilityObserver.observe(workspaceRef.value)
})

onUnmounted(() => {
  mounted = false
  loadGeneration += 1
  loadController?.abort()
  syncJobController?.abort()
  visibilityObserver?.disconnect()
  if (unsubscribeWS) unsubscribeWS()
  if (refreshTimer) window.clearTimeout(refreshTimer)
})

defineExpose({ reload: () => load(true) })

useRealtimeRefresh(() => loadEnabled ? load(true) : undefined)
</script>

<template>
  <section ref="workspaceRef" class="liked-workspace" aria-labelledby="liked-workspace-title">
    <header class="liked-workspace-header">
      <div>
        <span class="liked-workspace-kicker">
          {{ loading ? '收藏名单 · 正在加载' : `收藏名单 · ${summary.liked_models} 位` }}
        </span>
        <h3 id="liked-workspace-title">我的符合</h3>
        <p>你标为“符合”的模特都在这里。三步把她们缺的作品补进 115：校准库存 → 勾选模特 → 确认下载。</p>
      </div>
      <div class="liked-export-actions" aria-label="导出符合名单">
        <button type="button" class="btn secondary" :disabled="loading || !models.length"
                @click="exportJson">导出 JSON</button>
        <button type="button" class="btn secondary" :disabled="loading || !models.length"
                @click="exportCsv">导出 CSV</button>
      </div>
    </header>

    <div class="liked-safety-note">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
           aria-hidden="true"><path d="M12 3 4 7v5c0 5 3.4 8 8 9 4.6-1 8-4 8-9V7l-8-4Z"/><path d="m8.5 12 2.2 2.2 4.8-5"/></svg>
      <p><strong>只有点“加入下载”并确认后才会下载。</strong>下载完成后按现有规则自动上传到 115；查看、导出与校准库存都不会创建任务。</p>
    </div>

    <div class="liked-workflow" aria-label="正式使用流程">
      <article :class="{ complete: summary.unsynced_models === 0 }">
        <span>1</span>
        <div><strong>校准库存</strong><p>{{ summary.unsynced_models ? `${summary.unsynced_models} 位待处理` : '全部已校准' }}</p></div>
        <button type="button" class="btn secondary"
                :disabled="actionBusy || !summary.unsynced_models"
                @click="syncUnsynced">
          {{ syncProgress ? '校准中…' : `校准待处理 ${summary.unsynced_models || ''}`.trim() }}
        </button>
      </article>
      <article :class="{ complete: selected.size > 0 }">
        <span>2</span>
        <div><strong>审核并选择</strong><p>{{ selected.size ? `已选 ${selected.size} 位` : '查看准确缺失数后勾选' }}</p></div>
      </article>
      <article :class="{ complete: selectedDownloadCount > 0 }">
        <span>3</span>
        <div><strong>确认下载</strong><p>{{ selectedDownloadCount ? `${selectedDownloadCount} 个明确缺失` : '不会包含疑似已有' }}</p></div>
        <button type="button" class="btn amber"
                :disabled="actionBusy || selectedDownloadCount === 0"
                @click="queueSelected">
          {{ queueProgress ? '正在加入…' : `加入下载 ${selectedDownloadCount || ''}`.trim() }}
        </button>
      </article>
    </div>

    <div class="liked-summary" aria-label="符合名单汇总">
      <div><span>符合模特</span><strong>{{ summary.liked_models }}</strong></div>
      <div><span>已校准</span><strong>{{ summary.synced_models }}</strong></div>
      <div><span>明确缺失</span><strong>{{ summary.downloadable_videos }}</strong></div>
      <div><span>疑似已有</span><strong>{{ summary.maybe_videos }}</strong></div>
      <div><span>待上传</span><strong>{{ summary.pending_upload_videos }}</strong></div>
    </div>

    <div class="liked-live" aria-live="polite" aria-atomic="true">
      <p v-if="syncProgress" class="liked-progress-message" role="status">
        {{ syncProgress.label }}：{{ syncProgress.done }} / {{ syncProgress.total }}
      </p>
      <p v-else-if="queueProgress" class="liked-progress-message" role="status">
        正在加入下载：{{ queueProgress.done }} / {{ queueProgress.total }} 位，已新增 {{ queueProgress.added }} 个任务
      </p>
      <p v-else-if="notice" class="liked-notice" role="status">{{ notice }}</p>
      <div v-if="error" class="taste-error liked-error" role="alert">
        <span>{{ error }}</span>
        <button type="button" :disabled="loading" @click="load()">重新加载</button>
      </div>
    </div>

    <div v-if="loading" class="liked-workspace-loading" aria-busy="true">
      <span v-for="index in 3" :key="index"></span>
    </div>

    <template v-else-if="models.length">
      <div class="liked-toolbar">
        <label class="liked-search-field">
          <span class="sr-only">搜索符合模特</span>
          <input v-model="search" type="search" placeholder="搜索姓名或主页标识" />
        </label>
        <select v-model="sortBy" aria-label="符合名单排序">
          <option value="liked_desc">最近标记</option>
          <option value="unsynced_first">待校准优先</option>
          <option value="missing_desc">可下载最多</option>
          <option value="name">姓名排序</option>
        </select>
        <button type="button" class="btn secondary" :disabled="!filteredModels.length || actionBusy"
                @click="toggleFilteredSelection">
          {{ allFilteredSelected ? '取消当前全选' : `选择当前 ${filteredModels.length}` }}
        </button>
        <button v-if="selected.size" type="button" class="btn ghost"
                :disabled="actionBusy" @click="clearSelection">清空选择</button>
        <span class="liked-toolbar-spacer"></span>
        <button type="button" class="btn secondary"
                :disabled="actionBusy || !selected.size" @click="syncSelected">
          重新校准所选 {{ selected.size || '' }}
        </button>
      </div>

      <p v-if="selected.size" class="liked-selection-summary">
        已选 <strong>{{ selected.size }}</strong> 位，其中
        <strong>{{ selectedDownloadable.length }}</strong> 位共有
        <strong>{{ selectedDownloadCount }}</strong> 个明确缺失；
        <template v-if="selectedUnsyncedCount">{{ selectedUnsyncedCount }} 位尚未校准；</template>
        <template v-if="selectedWithoutMissingCount">{{ selectedWithoutMissingCount }} 位当前无需下载。</template>
      </p>

      <p v-if="payload?.stale" class="taste-model-warning" role="status">
        最近一次 115 目录扫描失败，当前绑定状态来自上一次成功快照；下载前建议重新校准。
      </p>

      <div v-if="filteredModels.length" class="liked-model-grid" :aria-busy="refreshing">
        <LikedModelCard v-for="model in visibleModels" :key="model.id"
                        :model="model" :selected="selected.has(model.id)"
                        :syncing="syncing.has(model.id)" :busy="Boolean(queueProgress)"
                        @toggle="toggleSelection" @sync="syncOne"
                        @download="queueOne" @detail="openDetail" />
      </div>
      <div v-else class="liked-no-results">
        没有找到“{{ search }}”；清空搜索后可查看全部 {{ models.length }} 位。
      </div>

      <div v-if="visibleModels.length < filteredModels.length" class="liked-load-more">
        <button type="button" class="btn secondary"
                @click="visibleLimit += 24">
          再显示 {{ Math.min(24, filteredModels.length - visibleModels.length) }} 位
        </button>
        <span>已显示 {{ visibleModels.length }} / {{ filteredModels.length }}</span>
      </div>
    </template>

    <div v-else-if="!error" class="liked-empty">
      <strong>还没有“符合”记录</strong>
      <p>在上方推荐里点“符合”，模特会立即出现在这里；不会自动同步、下载或上传。</p>
    </div>

    <VideoListModal v-if="detailModel" :model="detailModel" confirm-before-queue
                    @close="detailModel = null" @queued="onManualQueued" />
  </section>
</template>
