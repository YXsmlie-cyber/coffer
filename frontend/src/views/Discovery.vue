<script setup>
import { useRealtimeRefresh } from '../composables/useRealtimeRefresh'
import {
  computed, nextTick, onActivated, onDeactivated, onUnmounted, ref, watch,
} from 'vue'
import { useRoute, useRouter } from 'vue-router'
import api, { subscribeWS } from '../api'
import DiscoveryCard from '../components/DiscoveryCard.vue'
import VideoListModal from '../components/VideoListModal.vue'
import { confirm } from '../composables/useDialog'
import { useModelGrid } from '../composables/useModelGrid'
import { apiErrorText as errorText } from '../utils/apiError'
import { runModelSyncJob } from '../utils/modelSyncJob'

defineOptions({ name: 'Discovery' })

const route = useRoute()
const router = useRouter()
const sources = ref([])
const source = ref('home')
const maxCreators = ref(50)
const creators = ref([])
const loading = ref(false)
const initialLoading = ref(true)
const syncing = ref({})
const msg = ref('')
const msgType = ref('')
const search = ref('')
const searchResultModelId = ref(null)
const sortBy = ref('rank')
const profileTypeFilter = ref('all')
const detailModel = ref(null)
const fetchedAt = ref(null)
const activeSyncJobId = ref('')
const activeSyncLabel = ref('')
let loadToken = 0
let batchSyncIds = []
let pageActive = false
let initialized = false
let snapshotDirty = true
let snapshotPromise = null
let snapshotController = null
let syncJobController = null

const currentSource = computed(() =>
  sources.value.find(item => item.key === source.value) || null)
const typeFilteredCreators = computed(() => {
  if (profileTypeFilter.value === 'all') return creators.value
  return creators.value.filter(
    model => model.profile_type === profileTypeFilter.value,
  )
})
const profileTypeFilterLabel = computed(() => ({
  all: '全部上传者',
  model: '只看模特',
  channel: '只看频道',
})[profileTypeFilter.value] || '全部上传者')
const emptyResultText = computed(() => {
  const typeName = profileTypeFilter.value === 'model'
    ? '模特'
    : (profileTypeFilter.value === 'channel' ? '频道' : '上传者')
  const query = search.value.trim()
  if (query) return `当前${typeName}中没有匹配“${query}”的结果`
  return `当前结果中没有${typeName}`
})

function setMsg(text, type = 'accent') {
  msg.value = text
  msgType.value = type
}

function requestCanceled(error) {
  return error?.name === 'AbortError' || error?.code === 'ERR_CANCELED'
}

function pct(model) {
  if (!model.total_videos) return 0
  return Math.round(model.cloud_videos / model.total_videos * 100)
}

async function loadSources() {
  const { data } = await api.discoverySources()
  sources.value = data.sources || []
  const requested = String(route.query.source || '')
  if (sources.value.some(item => item.key === requested)) source.value = requested
  else if (!sources.value.some(item => item.key === source.value)) {
    source.value = sources.value[0]?.key || 'home'
  }
}

async function loadSnapshot({ quiet = false } = {}) {
  snapshotDirty = true
  if (!pageActive) return false
  snapshotController?.abort()
  if (snapshotPromise) return snapshotPromise
  snapshotPromise = (async () => {
    let loaded = false
    while (snapshotDirty && pageActive) {
      snapshotDirty = false
      const requestedSource = source.value
      const token = ++loadToken
      const controller = new AbortController()
      snapshotController = controller
      if (!quiet) initialLoading.value = true
      try {
        const { data } = await api.listDiscovery(requestedSource, { signal: controller.signal })
        if (token !== loadToken || source.value !== requestedSource || !pageActive) continue
        creators.value = data.creators || []
        fetchedAt.value = data.fetched_at || null
        loaded = true
      } catch (error) {
        if (error.code !== 'ERR_CANCELED' && token === loadToken) {
          setMsg('加载发现结果失败：' + errorText(error), 'err')
        }
      } finally {
        if (token === loadToken) initialLoading.value = false
      }
    }
    return loaded
  })()
  try {
    return await snapshotPromise
  } finally {
    snapshotPromise = null
    snapshotController = null
    if (snapshotDirty && pageActive) void loadSnapshot({ quiet: true })
  }
}

function limitIsValid() {
  const value = Number(maxCreators.value)
  return Number.isInteger(value) && value >= 1 && value <= 500
}

async function syncOne(modelId, reload = true, throwOnError = false) {
  syncing.value = { ...syncing.value, [modelId]: true }
  try {
    await api.syncModel(modelId)
    if (reload) await loadSnapshot({ quiet: true })
  } catch (error) {
    if (reload) setMsg('同步失败：' + errorText(error), 'err')
    if (throwOnError) throw error
  } finally {
    syncing.value = { ...syncing.value, [modelId]: false }
  }
}

function applyBatchSyncStatus(status) {
  if (!status || status.job_id !== activeSyncJobId.value) return
  const active = new Set(status.active_model_ids || [])
  const next = { ...syncing.value }
  for (const id of batchSyncIds) next[id] = active.has(id)
  syncing.value = next
  setMsg(`${activeSyncLabel.value}（${status.done || 0}/${status.total || 0}）`)
}

async function syncMany(ids, label) {
  const uniqueIds = [...new Set(ids)]
  if (!uniqueIds.length) return { completed: 0, failed: [] }
  batchSyncIds = uniqueIds
  activeSyncLabel.value = label
  const controller = new AbortController()
  syncJobController?.abort()
  syncJobController = controller
  if (!pageActive) controller.abort()
  try {
    return await runModelSyncJob(uniqueIds, {
      onCreated(status) {
        activeSyncJobId.value = status.job_id
        applyBatchSyncStatus(status)
      },
      onProgress: applyBatchSyncStatus,
      onRefresh() { return loadSnapshot({ quiet: true }) },
      shouldPoll: () => pageActive,
      signal: controller.signal,
      onConnectionState(connection) {
        if (connection.state === 'retrying') {
          setMsg(`${label}（网络暂时中断，后台任务仍在运行，正在第 ${connection.attempt} 次重连）`)
        }
      },
    })
  } finally {
    if (syncJobController === controller) syncJobController = null
    const next = { ...syncing.value }
    for (const id of batchSyncIds) next[id] = false
    syncing.value = next
    batchSyncIds = []
    activeSyncJobId.value = ''
    activeSyncLabel.value = ''
  }
}

async function fetchSource() {
  if (!limitIsValid()) {
    setMsg('获取数量必须是 1～500 之间的整数', 'err')
    return
  }
  loading.value = true
  const requestedMax = Number(maxCreators.value)
  setMsg(`正在获取「${currentSource.value?.label || source.value}」（最多 ${requestedMax} 位）`)
  try {
    const { data } = await api.scrapeDiscovery(source.value, requestedMax)
    await loadSnapshot({ quiet: true })
    if (!data.model_ids?.length) {
      setMsg('当前来源没有可同步的上传者；可能暂时没有进行中的内容', '')
      return
    }
    const result = await syncMany(data.model_ids, '已获取，正在同步完整作品并与 115 对比')
    const discoveryText = data.reached_limit
      ? `已达到设置上限，共获取 ${data.creator_count} 位`
      : `已读取 ${data.page_count || 1} 页，共发现 ${data.creator_count} 位（少于上限 ${requestedMax} 位）`
    setMsg(result.failed.length
      ? `${discoveryText}；${result.completed} 位同步成功，${result.failed.length} 位失败`
      : `${discoveryText}；${result.completed} 位同步成功`,
    result.failed.length ? 'err' : '')
  } catch (error) {
    if (!requestCanceled(error)) setMsg('获取失败：' + errorText(error), 'err')
  } finally {
    loading.value = false
  }
}

async function resyncAll() {
  if (!creators.value.length) return
  loading.value = true
  try {
    const result = await syncMany(creators.value.map(item => item.id), '正在重新同步当前来源')
    setMsg(result.failed.length
      ? `${result.completed} 位同步成功，${result.failed.length} 位失败`
      : `当前来源 ${result.completed} 位上传者已全部同步`, result.failed.length ? 'err' : '')
  } catch (error) {
    if (!requestCanceled(error)) setMsg('重新同步失败：' + errorText(error), 'err')
  } finally {
    loading.value = false
  }
}

async function download(model, mode) {
  try {
    const { data } = await api.addDownload(model.id, mode)
    if (data.added > 0) setMsg(`已添加 ${data.added} 个下载任务`)
    else if ((model.pending_upload_videos || 0) > 0) {
      setMsg(`${model.pending_upload_videos} 个视频已下载，正在等待上传`)
    } else setMsg('没有新的可下载视频', '')
    await loadSnapshot({ quiet: true })
  } catch (error) {
    setMsg('添加失败：' + errorText(error), 'err')
  }
}

function openDetail(model) {
  if (model.synced_at) detailModel.value = model
}

async function removeModel(model) {
  if (!await confirm({
    title: '删除上传者',
    message: `删除「${model.name}」及其同步记录？（不影响已下载/已上传文件）`,
    danger: true,
    confirmText: '删除',
  })) return
  try {
    await api.deleteModel(model.id)
    creators.value = creators.value.filter(item => item.id !== model.id)
    setMsg(`已删除 ${model.name}`)
  } catch (error) {
    setMsg('删除失败：' + errorText(error), 'err')
  }
}

function clearSearch() {
  search.value = ''
}

function onQueued(count) {
  setMsg(`已添加 ${count} 个下载任务`)
  void loadSnapshot({ quiet: true })
}

function onWS(message) {
  if (message.type === 'library_membership_changed') {
    snapshotDirty = true
    void loadSnapshot({ quiet: true })
    return
  }
  if (message.type === 'model_sync_progress'
      && message.job_id === activeSyncJobId.value) {
    if (message.model_id && message.phase === 'started') {
      syncing.value = { ...syncing.value, [message.model_id]: true }
    } else if (message.model_id && message.phase === 'finished') {
      syncing.value = { ...syncing.value, [message.model_id]: false }
    }
    setMsg(`${activeSyncLabel.value}（${message.done || 0}/${message.total || 0}）`)
    return
  }
  if (message.type !== 'model_update' || !message.model) return
  const index = creators.value.findIndex(item => item.id === message.model.id)
  if (index < 0) return
  const previous = creators.value[index]
  const uploaded = Math.max(0,
    (previous.missing_videos || 0)
    - (message.model.missing_videos ?? previous.missing_videos ?? 0))
  creators.value[index] = {
    ...previous,
    ...message.model,
    pending_upload_videos: Math.max(
      0, (previous.pending_upload_videos || 0) - uploaded),
  }
  if (snapshotPromise) snapshotDirty = true
}

const {
  displayModels, pagedModels, hasMore, sentinel, modelGrid,
} = useModelGrid({
  models: typeFilteredCreators,
  search,
  searchResultModelId,
  sortBy,
  progressOf: pct,
  resetSources: [profileTypeFilter],
})

async function selectSource() {
  search.value = ''
  await router.replace({
    name: 'discovery',
    query: { ...route.query, source: source.value },
  })
  await nextTick()
  await loadSnapshot()
}

watch(() => route.query.source, async value => {
  const nextSource = String(value || '')
  if (!nextSource || nextSource === source.value
      || !sources.value.some(item => item.key === nextSource)) return
  source.value = nextSource
  search.value = ''
  await loadSnapshot()
})

let unsubscribeWS = null
onActivated(async () => {
  pageActive = true
  unsubscribeWS ||= subscribeWS(onWS)
  try {
    if (!initialized) {
      await loadSources()
      initialized = true
    }
    await loadSnapshot()
  } catch (error) {
    initialLoading.value = false
    setMsg('初始化视频发现页失败：' + errorText(error), 'err')
  }
})
onDeactivated(() => {
  pageActive = false
  snapshotDirty = true
  loadToken += 1
  snapshotController?.abort()
  unsubscribeWS?.()
  unsubscribeWS = null
})
onUnmounted(() => {
  pageActive = false
  snapshotController?.abort()
  syncJobController?.abort()
  unsubscribeWS?.()
})

useRealtimeRefresh(() => loadSnapshot({ quiet: true }))
</script>

<template>
  <div class="page-title">
    <h2>视频发现</h2>
    <div class="sub">从主页与视频栏目发现更多上传者，确认后按整库同步并下载 115 中缺失的视频</div>
  </div>

  <div class="toolbar discovery-toolbar">
    <label class="sr-only" for="discovery-source">视频来源</label>
    <select id="discovery-source" v-model="source" :disabled="loading"
            title="选择视频来源" @change="selectSource">
      <option v-for="item in sources" :key="item.key" :value="item.key">{{ item.label }}</option>
    </select>
    <input v-model.number="maxCreators" type="number" min="1" max="500" :disabled="loading"
           aria-label="最多获取的上传者数量（1 到 500）" title="最多获取 500 位上传者" />
    <span class="discovery-limit-unit">位上传者上限</span>
    <button class="btn" :disabled="loading || !currentSource" @click="fetchSource">
      {{ loading ? '处理中' : '获取并同步' }}
    </button>
    <button class="btn secondary" :disabled="loading || !creators.length" @click="resyncAll">
      重新同步当前
    </button>
    <a v-if="currentSource" class="source-link" :href="currentSource.url"
       target="_blank" rel="noopener noreferrer" title="在 PornHub 打开当前来源">打开源页面</a>
  </div>

  <div class="toast-region" aria-live="polite" aria-atomic="true">
    <div v-if="msg" class="toast" :class="msgType"
         :role="msgType === 'err' ? 'alert' : 'status'">{{ msg }}</div>
  </div>

  <section class="model-search-panel discovery-filter-panel">
    <div class="model-search-form">
      <label class="model-search-label" for="discovery-search">筛选当前结果</label>
      <div class="model-search-row">
        <input id="discovery-search" v-model="search" class="search-input" type="search"
               autocomplete="off" spellcheck="false" placeholder="按名字或账号筛选" />
        <button class="btn secondary" type="button" :disabled="!search" @click="clearSearch">清除</button>
        <select v-model="profileTypeFilter" class="discovery-type-filter"
                aria-label="上传者类型筛选">
          <option value="all">全部</option>
          <option value="model">只看模特</option>
          <option value="channel">只看频道</option>
        </select>
        <select v-model="sortBy" class="model-search-sort" aria-label="发现结果排序方式">
          <option value="rank">按来源顺序</option>
          <option value="missing">缺失优先</option>
          <option value="progress">同步率优先</option>
          <option value="name">按名字</option>
        </select>
      </div>
      <div class="model-search-meta">
        <span>{{ currentSource?.label || '当前来源' }} · {{ profileTypeFilterLabel }} · 同一上传者的多个封面已合并</span>
        <span>{{ displayModels.length }} / {{ creators.length }} 位</span>
      </div>
    </div>
  </section>

  <div v-if="initialLoading" class="discovery-grid">
    <div v-for="index in 6" :key="index" class="skeleton discovery-skeleton"></div>
  </div>
  <div v-else-if="!creators.length" class="empty">
    当前来源暂无发现结果；点击“获取并同步”刷新
  </div>
  <div v-else-if="!displayModels.length" class="empty">
    {{ emptyResultText }}
  </div>

  <div v-else ref="modelGrid" data-model-grid>
    <TransitionGroup name="card" tag="div" class="discovery-grid">
      <DiscoveryCard v-for="model in pagedModels" :key="model.id"
                     :model="model" :syncing="Boolean(syncing[model.id])"
                     @download="download" @detail="openDetail"
                     @sync="syncOne" @remove="removeModel" />
    </TransitionGroup>
  </div>

  <div v-if="!initialLoading && displayModels.length" ref="sentinel" class="load-sentinel">
    <span v-if="hasMore" class="hint">
      向下滚动加载更多 · 已显示 {{ pagedModels.length }} / {{ displayModels.length }}
    </span>
    <span v-else class="hint">已显示全部 {{ displayModels.length }} 位</span>
  </div>

  <VideoListModal v-if="detailModel" :model="detailModel"
                  @close="detailModel = null" @queued="onQueued" />
</template>
