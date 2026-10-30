<script setup>
import { useRealtimeRefresh } from '../composables/useRealtimeRefresh'
import {
  computed, nextTick, onActivated, onDeactivated, onUnmounted, ref, watch,
} from 'vue'
import api, { subscribeWS } from '../api'
import ModelCard from '../components/ModelCard.vue'
import ModelDrawer from '../components/ModelDrawer.vue'
import { confirm } from '../composables/useDialog'
import { useModelGrid } from '../composables/useModelGrid'
import { apiErrorText as errorText } from '../utils/apiError'
import { runModelSyncJob } from '../utils/modelSyncJob'

defineOptions({ name: 'Models' })

const rankingUrl = ref('https://cn.pornhub.com/pornstars?gender=female')
const MAX_RANKING_MODELS = 500
const maxModels = ref(50)
const rankingInputInvalid = ref(false)

// 榜单来源预设。pornstars 类支持排序；discover 类是视频流（按指标排序），提取上传者。
// 注意：经实测，PH 现行榜单的 gender=female 并不能完全过滤男性，discover 类更是不支持，
// 因此任何来源的结果都可能混入男性/频道，需自行在列表中挑选。
const RANKING_SOURCES = [
  { label: '女明星 · 人气', url: 'https://cn.pornhub.com/pornstars?gender=female' },
  { label: '女明星 · 评分最高', url: 'https://cn.pornhub.com/pornstars?o=r&gender=female' },
  { label: '女明星 · 趋势', url: 'https://cn.pornhub.com/pornstars?o=t&gender=female' },
  { label: '发现 · 最多订阅模特', url: 'https://cn.pornhub.com/user/discover/most_subscribed_models' },
  { label: '发现 · 最多订阅素人', url: 'https://cn.pornhub.com/user/discover/most_subscribed_amateurs' },
  { label: '发现 · 最多订阅用户', url: 'https://cn.pornhub.com/user/discover/most_subscribed_users' },
  { label: '发现 · 热门内容伙伴', url: 'https://cn.pornhub.com/user/discover/popular_content_partners' },
  { label: '发现 · 最高评分视频', url: 'https://cn.pornhub.com/user/discover/highest_rated_videos' },
]
const sourcePreset = ref(RANKING_SOURCES[0].url)
function onSourceChange() {
  if (sourcePreset.value) rankingUrl.value = sourcePreset.value
}
const models = ref([])
const totalModels = ref(0)
const nextCursor = ref(null)
const hasMore = ref(false)
const pageIndex = ref(0)
const pageCursor = ref(null)
const legacyMode = ref(false)
const loading = ref(false)
const initialLoading = ref(true)
const pageLoading = ref(false)
const syncing = ref({})
const msg = ref('')
const msgType = ref('')
const detailId = ref(null)
const sourcePanelOpen = ref(false)
const search = ref('')
const searchResultModelId = ref(null)
const searchBusy = ref(false)
const searchPhase = ref('')
const searchStatus = ref('')
const searchError = ref('')
const sortBy = ref('rank')   // rank / missing / progress / name
const activeSyncJobId = ref('')
const activeSyncLabel = ref('')
let batchSyncIds = []
let cursorHistory = [null]
let searchTimer = null
let pageActive = false
let loadDirty = true
let loadPromise = null
let loadController = null
let loadGeneration = 0
let pagingGeneration = 0
let syncJobController = null
let legacyAllModels = []

function legacyFilteredModels() {
  const query = search.value.trim().toLocaleLowerCase()
  let result = query
    ? legacyAllModels.filter(model => [model.name, model.ph_id, model.profile_slug]
      .some(value => String(value || '').toLocaleLowerCase().includes(query)))
    : [...legacyAllModels]
  if (sortBy.value === 'missing') {
    result.sort((left, right) => Number(right.missing_videos || 0) - Number(left.missing_videos || 0)
      || Number(left.id) - Number(right.id))
  } else if (sortBy.value === 'progress') {
    result.sort((left, right) => pct(right) - pct(left) || Number(left.id) - Number(right.id))
  } else if (sortBy.value === 'name') {
    result.sort((left, right) => String(left.name || '').localeCompare(
      String(right.name || ''), 'zh-CN') || Number(left.id) - Number(right.id))
  }
  return result
}

function applyLegacyPage() {
  const filtered = legacyFilteredModels()
  const start = pageIndex.value * 60
  models.value = filtered.slice(start, start + 60)
  totalModels.value = filtered.length
  nextCursor.value = null
  hasMore.value = start + models.value.length < filtered.length
}

async function loadModels({ reset = false, supersede = false } = {}) {
  if (reset) {
    pagingGeneration += 1
    pageIndex.value = 0
    pageCursor.value = null
    cursorHistory = [null]
  }
  loadDirty = true
  if (supersede) loadController?.abort()
  if (!pageActive) return false
  if (loadPromise) return loadPromise

  loadPromise = (async () => {
    let loaded = false
    while (loadDirty && pageActive) {
      // 返回值代表最后一次（最新筛选/游标）请求，而不是本轮任意旧请求。
      loaded = false
      loadDirty = false
      const generation = ++loadGeneration
      const controller = new AbortController()
      loadController = controller
      if (!models.value.length) initialLoading.value = true
      try {
        const options = {
          limit: 60,
          query: search.value.trim() || undefined,
          sort: sortBy.value,
        }
        if (pageCursor.value) options.cursor = pageCursor.value
        const { data } = await api.listModels(options, { signal: controller.signal })
        if (generation !== loadGeneration || !pageActive) continue
        if (data.total === undefined) {
          // 无停机发布期间旧后端会忽略分页参数；只挂载当前 60 张卡片，
          // 暂以本地分页兼容，后端重启后自动切回游标模式。
          legacyMode.value = true
          legacyAllModels = data.models || []
          applyLegacyPage()
        } else {
          // 无停机升级期间可能正停在旧后端的本地第 N 页；新后端第一次
          // 响应没有对应 cursor，实际内容是第1页，必须同步重置页码/历史。
          if (legacyMode.value && pageIndex.value > 0 && !pageCursor.value) {
            pagingGeneration += 1
            pageIndex.value = 0
            pageCursor.value = null
            cursorHistory = [null]
          }
          legacyMode.value = false
          legacyAllModels = []
          models.value = data.models || []
          totalModels.value = Number(data.total ?? models.value.length)
          nextCursor.value = data.next_cursor ?? null
          hasMore.value = Boolean(data.has_more ?? nextCursor.value)
        }
        loaded = true
      } catch (error) {
        if (error.code !== 'ERR_CANCELED' && generation === loadGeneration) {
          setMsg('加载模特列表失败：' + errorText(error), 'err')
        }
      } finally {
        if (generation === loadGeneration) initialLoading.value = false
      }
    }
    return loaded
  })()
  try {
    return await loadPromise
  } finally {
    loadPromise = null
    loadController = null
    if (loadDirty && pageActive) void loadModels()
  }
}

async function nextPage() {
  if (pageLoading.value || !hasMore.value) return
  if (legacyMode.value) {
    pageIndex.value += 1
    applyLegacyPage()
    window.scrollTo({ top: 0, behavior: 'auto' })
    return
  }
  if (!nextCursor.value) return
  pageLoading.value = true
  const navigation = ++pagingGeneration
  const targetIndex = pageIndex.value + 1
  const targetCursor = nextCursor.value
  cursorHistory[targetIndex] = targetCursor
  pageIndex.value = targetIndex
  pageCursor.value = targetCursor
  try {
    const loaded = await loadModels({ supersede: true })
    if (loaded) {
      window.scrollTo({ top: 0, behavior: 'auto' })
    } else if (pagingGeneration === navigation
        && pageIndex.value === targetIndex && pageCursor.value === targetCursor) {
      pageIndex.value = targetIndex - 1
      pageCursor.value = cursorHistory[pageIndex.value] || null
      cursorHistory.length = pageIndex.value + 1
    }
  } finally {
    pageLoading.value = false
  }
}

async function previousPage() {
  if (pageLoading.value || pageIndex.value <= 0) return
  pageLoading.value = true
  const navigation = ++pagingGeneration
  const previousIndex = pageIndex.value
  const previousCursor = pageCursor.value
  pageIndex.value -= 1
  if (legacyMode.value) {
    applyLegacyPage()
    pageLoading.value = false
    window.scrollTo({ top: 0, behavior: 'auto' })
    return
  }
  const targetIndex = pageIndex.value
  const targetCursor = cursorHistory[targetIndex] || null
  pageCursor.value = targetCursor
  try {
    const loaded = await loadModels({ supersede: true })
    if (loaded) {
      window.scrollTo({ top: 0, behavior: 'auto' })
    } else if (pagingGeneration === navigation
        && pageIndex.value === targetIndex && pageCursor.value === targetCursor) {
      pageIndex.value = previousIndex
      pageCursor.value = previousCursor
    }
  } finally {
    pageLoading.value = false
  }
}

function setMsg(text, type = 'accent') { msg.value = text; msgType.value = type }
function requestCanceled(error) {
  return error?.name === 'AbortError' || error?.code === 'ERR_CANCELED'
}

function rankingLimitMessage() {
  const currentValue = maxModels.value === '' || maxModels.value == null ? '空' : maxModels.value
  return `爬取数量必须是 1～${MAX_RANKING_MODELS} 之间的整数（当前输入：${currentValue}）`
}

function maxModelsIsValid() {
  const value = Number(maxModels.value)
  return Number.isInteger(value) && value >= 1 && value <= MAX_RANKING_MODELS
}

function onMaxModelsInput() {
  if (!rankingInputInvalid.value) return
  if (maxModelsIsValid()) {
    rankingInputInvalid.value = false
    if (msg.value.startsWith('爬取数量')) setMsg('', '')
  } else {
    setMsg(rankingLimitMessage(), 'err')
  }
}

function localExactModel(query) {
  const name = ' '.concat(query).trim().replace(/\s+/g, ' ').toLocaleLowerCase()
  const phId = name.replace(/^@/, '')
  return models.value.find(model =>
    model.name.trim().replace(/\s+/g, ' ').toLocaleLowerCase() === name
    || model.ph_id?.toLocaleLowerCase() === phId)
}

function mergeLookupCard(model) {
  const card = {
    ph_video_count: 0,
    views_text: '',
    total_videos: 0,
    cloud_videos: 0,
    maybe_videos: 0,
    missing_videos: 0,
    pending_upload_videos: 0,
    downloadable_videos: 0,
    ...model,
  }
  const index = models.value.findIndex(item => item.id === card.id)
  if (index < 0) models.value = [...models.value, card]
  else models.value[index] = { ...models.value[index], ...card }
  return models.value.find(item => item.id === card.id)
}

async function revealSearchResult(modelId) {
  await nextTick()
  document.querySelector(`[data-model-id="${modelId}"]`)
    ?.scrollIntoView({ block: 'nearest', inline: 'nearest', behavior: 'auto' })
}

async function syncSearchResult(model, created) {
  searchPhase.value = 'sync'
  try {
    await syncOne(model.id, false, true)
    await loadModels()
    searchStatus.value = `${model.name} 已完成视频同步与 115 对比`
    await revealSearchResult(model.id)
    return true
  } catch (error) {
    searchError.value = `${created ? '已添加' : '已找到'} ${model.name}，但同步失败：${errorText(error)}。可点击卡片上的“同步”重试。`
    return false
  }
}

async function submitModelSearch() {
  if (searchBusy.value) return
  const query = search.value.trim().replace(/\s+/g, ' ')
  searchError.value = ''
  searchStatus.value = ''
  if (!query) {
    searchError.value = '请输入精确的模特名字或账号'
    return
  }

  searchBusy.value = true
  try {
    const local = localExactModel(query)
    if (local) {
      searchResultModelId.value = local.id
      await revealSearchResult(local.id)
      if (local.synced_at) {
        searchStatus.value = `已从本地找到 ${local.name}`
      } else {
        await syncSearchResult(local, false)
      }
      return
    }

    searchPhase.value = 'lookup'
    const { data } = await api.lookupModel(query)
    let model = mergeLookupCard(data.model)
    searchResultModelId.value = model.id
    await loadModels()
    model = models.value.find(item => item.id === data.model.id) || model
    await revealSearchResult(model.id)
    if (!model.synced_at) {
      await syncSearchResult(model, data.created)
    } else {
      searchStatus.value = `已找到 ${model.name}，本地同步数据保持不变`
    }
  } catch (error) {
    searchResultModelId.value = null
    searchError.value = errorText(error)
  } finally {
    searchPhase.value = ''
    searchBusy.value = false
  }
}

function onSearchInput() {
  searchResultModelId.value = null
  searchStatus.value = ''
  searchError.value = ''
  window.clearTimeout(searchTimer)
  searchTimer = window.setTimeout(() => {
    void loadModels({ reset: true, supersede: true })
  }, 250)
}

function clearModelSearch() {
  window.clearTimeout(searchTimer)
  search.value = ''
  searchResultModelId.value = null
  searchStatus.value = ''
  searchError.value = ''
  searchPhase.value = ''
  void loadModels({ reset: true, supersede: true })
}

async function scrape() {
  const requestedMax = Number(maxModels.value)
  if (!maxModelsIsValid()) {
    rankingInputInvalid.value = true
    setMsg(rankingLimitMessage(), 'err')
    return
  }
  rankingInputInvalid.value = false
  loading.value = true
  setMsg('正在爬取榜单')
  try {
    const { data } = await api.scrapeRanking(rankingUrl.value, requestedMax)
    await loadModels()
    const result = await syncMany(data.models.map(m => m.id), '爬取完成，正在与 115 对比')
    const avatarFailed = data.avatar_failures?.length || 0
    setMsg(result.failed.length
      ? `榜单已爬取；${result.completed} 位同步成功，${result.failed.length} 位同步失败${avatarFailed ? `，${avatarFailed} 位头像未通过源站校验` : ''}`
      : `同步完成，共 ${result.completed} 位${avatarFailed ? `；另有 ${avatarFailed} 位头像未通过源站校验，未写入` : ''}`,
    result.failed.length || avatarFailed ? 'err' : '')
  } catch (e) {
    if (!requestCanceled(e)) setMsg('爬取失败：' + errorText(e), 'err')
  } finally {
    loading.value = false
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

// 批量并发、115 根目录快照与写库限流统一由后端协调；页面只创建一个任务，
// WebSocket 即时更新，状态查询负责断线期间的进度与最终结果兜底。
async function syncMany(ids, label) {
  const uniqueIds = [...new Set(ids)]
  if (!uniqueIds.length) return { completed: 0, failed: [] }
  batchSyncIds = uniqueIds
  activeSyncLabel.value = label
  const controller = new AbortController()
  syncJobController?.abort()
  syncJobController = controller
  // 榜单请求可能在页面失活后才返回；不要在已缓存的后台页面中新建一个
  // 永远等待 shouldPoll() 的轮询循环。
  if (!pageActive) controller.abort()
  try {
    return await runModelSyncJob(uniqueIds, {
      onCreated(status) {
        activeSyncJobId.value = status.job_id
        applyBatchSyncStatus(status)
      },
      onProgress: applyBatchSyncStatus,
      onRefresh() { return loadModels() },
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

async function syncOne(modelId, reload = true, throwOnError = false) {
  syncing.value = { ...syncing.value, [modelId]: true }
  try {
    await api.syncModel(modelId)
    if (reload) await loadModels()
  } catch (e) {
    console.error('同步失败', modelId, e)
    if (reload) setMsg('同步失败：' + errorText(e), 'err')
    if (throwOnError) throw e
  } finally {
    syncing.value = { ...syncing.value, [modelId]: false }
  }
}

async function resyncAll() {
  if (!totalModels.value) return
  loading.value = true
  try {
    // “全部”是显式重操作；只在用户点击时取兼容的完整 ID 快照。
    const allModels = legacyMode.value && legacyAllModels.length
      ? legacyAllModels
      : (await api.listModels()).data.models || []
    const result = await syncMany(allModels.map(m => m.id), '正在重新同步全部')
    setMsg(result.failed.length
      ? `${result.completed} 位同步成功，${result.failed.length} 位失败`
      : `全部 ${result.completed} 位同步完成`, result.failed.length ? 'err' : '')
  } catch (error) {
    if (!requestCanceled(error)) setMsg('重新同步失败：' + errorText(error), 'err')
  } finally {
    loading.value = false
  }
}

async function download(model, mode) {
  try {
    const { data } = await api.addDownload(model.id, mode)
    if (data.added > 0) {
      setMsg(`已添加 ${data.added} 个下载任务`)
    } else if ((model.pending_upload_videos || 0) > 0) {
      setMsg(`${model.pending_upload_videos} 个视频已下载，正在等待上传，无需重复下载`, '')
    } else {
      setMsg('没有新的可下载视频', '')
    }
    await loadModels()
  } catch (e) {
    setMsg('添加失败：' + errorText(e), 'err')
  }
}

async function removeModel(m) {
  if (!await confirm({ title: '删除模特',
        message: `删除模特「${m.name}」及其视频记录？（不影响已下载/已上传的文件）`,
        danger: true, confirmText: '删除' })) return
  try {
    await api.deleteModel(m.id)
    models.value = models.value.filter(x => x.id !== m.id)
    if (legacyMode.value) {
      legacyAllModels = legacyAllModels.filter(model => model.id !== m.id)
      applyLegacyPage()
      if (!models.value.length && pageIndex.value > 0) {
        pageIndex.value -= 1
        applyLegacyPage()
      }
    } else {
      totalModels.value = Math.max(0, totalModels.value - 1)
      if (!models.value.length && pageIndex.value > 0) await previousPage()
    }
    if (searchResultModelId.value === m.id) clearModelSearch()
    setMsg(`已删除 ${m.name}`)
  } catch (e) {
    setMsg('删除失败：' + errorText(e), 'err')
  }
}

function pct(m) {
  if (!m.total_videos) return 0
  return Math.round(m.cloud_videos / m.total_videos * 100)
}

// 详情抽屉跟随当前页列表中的同一对象，WebSocket 合并的统计会即时反映；
// 模特被删除或翻页后对象消失，抽屉随之关闭。
const drawerIndex = computed(() => models.value.findIndex(model => model.id === detailId.value))
const drawerModel = computed(() => models.value[drawerIndex.value] || null)
const sourcePanelVisible = computed(() => sourcePanelOpen.value
  || (!initialLoading.value && !totalModels.value && !search.value.trim()))

watch(drawerModel, model => {
  if (!model && detailId.value !== null) detailId.value = null
})

function cardElement(modelId) {
  return document.querySelector(`[data-model-id="${modelId}"]`)
}

function openDetail(m) {
  setMsg('', '')
  detailId.value = m.id
}

function stepDetail(delta) {
  const next = models.value[drawerIndex.value + delta]
  if (!next) return
  detailId.value = next.id
  cardElement(next.id)?.scrollIntoView({ block: 'nearest', inline: 'nearest', behavior: 'auto' })
}

async function closeDetail() {
  const modelId = detailId.value
  detailId.value = null
  await nextTick()
  // 上一位/下一位切换后，焦点回到当前查看的卡片，而不是最初点击的那张。
  cardElement(modelId)?.querySelector('.media-card-hit')?.focus({ preventScroll: true })
}

// 抽屉内勾选下载由 VideoList 就地提示；这里只刷新待上传等统计。
function onQueued() { void loadModels() }

// 上传成功/二次去重命中后，后端广播 model_update，实时更新对应卡片的缺失/已有数
let unsubWS = null
function onWS(msg) {
  if (msg.type === 'library_membership_changed') {
    loadDirty = true
    if (pageActive) void loadModels()
    return
  }
  if (msg.type === 'model_sync_progress'
      && msg.job_id === activeSyncJobId.value) {
    if (msg.model_id && msg.phase === 'started') {
      syncing.value = { ...syncing.value, [msg.model_id]: true }
    } else if (msg.model_id && msg.phase === 'finished') {
      syncing.value = { ...syncing.value, [msg.model_id]: false }
    }
    setMsg(`${activeSyncLabel.value}（${msg.done || 0}/${msg.total || 0}）`)
    return
  }
  if (msg.type !== 'model_update' || !msg.model) return
  if (legacyMode.value) {
    const legacyIndex = legacyAllModels.findIndex(model => model.id === msg.model.id)
    if (legacyIndex >= 0) {
      legacyAllModels[legacyIndex] = { ...legacyAllModels[legacyIndex], ...msg.model }
    }
  }
  const i = models.value.findIndex(m => m.id === msg.model.id)
  if (i >= 0) {
    const previous = models.value[i]
    const uploaded = Math.max(0,
      (previous.missing_videos || 0) - (msg.model.missing_videos ?? previous.missing_videos ?? 0))
    models.value[i] = {
      ...previous,
      ...msg.model,
      // 上传成功的 WebSocket 更新会先减少 missing；同步扣减待上传数，
      // 完整刷新时仍以后端聚合值为准。
      pending_upload_videos: Math.max(0, (previous.pending_upload_videos || 0) - uploaded),
    }
    // 若 REST 快照与这条增量交叉到达，完成当前请求后再取一次权威页，
    // 防止较早的响应把刚合并的 WebSocket 状态覆盖回去。
    if (loadPromise) loadDirty = true
  }
}

watch(sortBy, () => {
  void loadModels({ reset: true, supersede: true })
})

// 服务端分页保持 DOM 有界；复用网格锚点逻辑只负责 KeepAlive 返回时的位置恢复。
const gridSearch = ref('')
const gridResult = ref(null)
const gridSort = ref('rank')
const { modelGrid } = useModelGrid({
  models,
  search: gridSearch,
  searchResultModelId: gridResult,
  sortBy: gridSort,
  progressOf: pct,
})

onActivated(() => {
  pageActive = true
  unsubWS ||= subscribeWS(onWS)
  void loadModels()
})
onDeactivated(() => {
  pageActive = false
  // 浏览器后退可绕过遮罩离开页面；返回时不保留已失去上下文的抽屉。
  detailId.value = null
  loadDirty = true
  loadGeneration += 1
  loadController?.abort()
  unsubWS?.()
  unsubWS = null
})
onUnmounted(() => {
  pageActive = false
  window.clearTimeout(searchTimer)
  loadController?.abort()
  syncJobController?.abort()
  unsubWS?.()
})

useRealtimeRefresh(() => loadModels())
</script>

<template>
  <header class="media-head">
    <div class="media-head-title">
      <h2>模特</h2>
      <p class="sub">PornHub 榜单 · 按名次与 115 网盘对比缺失视频</p>
    </div>
    <button type="button" class="btn secondary" :aria-expanded="sourcePanelVisible"
            aria-controls="ranking-panel" @click="sourcePanelOpen = !sourcePanelVisible">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
           stroke-linecap="round" aria-hidden="true">
        <path v-if="sourcePanelVisible" d="m6 15 6-6 6 6" /><path v-else d="M12 5v14M5 12h14" />
      </svg>
      {{ sourcePanelVisible ? '收起榜单' : '爬取榜单' }}
    </button>
  </header>

  <section v-show="sourcePanelVisible" id="ranking-panel" class="source-panel"
           aria-label="爬取榜单">
    <div class="toolbar ranking-toolbar">
      <select v-model="sourcePreset" @change="onSourceChange" style="width:180px" title="选择榜单来源">
        <option v-for="s in RANKING_SOURCES" :key="s.url" :value="s.url">{{ s.label }}</option>
      </select>
      <input v-model="rankingUrl" class="ranking-url" placeholder="榜单 URL（可手动编辑）"
             aria-label="榜单 URL" />
      <input v-model.number="maxModels" type="number" min="1" :max="MAX_RANKING_MODELS"
             style="width:84px" title="每次最多爬取 500 位模特"
             aria-label="最多模特数（1 到 500）"
             aria-describedby="max-models-help ranking-feedback"
             :aria-invalid="rankingInputInvalid" @input="onMaxModelsInput" />
      <span id="max-models-help" class="sr-only">每次可爬取 1 到 500 位模特</span>
      <button class="btn" :disabled="loading" @click="scrape">
        {{ loading ? '处理中' : '爬取榜单' }}
      </button>
      <button class="btn secondary" :disabled="loading || !totalModels" @click="resyncAll">
        重新同步全部
      </button>
    </div>
    <p class="source-panel-note">榜单可能混入男性或频道；爬取后会自动与 115 对比，结果按名次排列。</p>
  </section>

  <div id="ranking-feedback" class="toast-region" aria-live="polite" aria-atomic="true">
    <div v-if="msg && !drawerModel" class="toast" :class="msgType"
         :role="msgType === 'err' ? 'alert' : 'status'">{{ msg }}</div>
  </div>

  <div class="media-filter-bar">
    <form class="media-filter" :aria-busy="searchBusy" @submit.prevent="submitModelSearch">
      <label class="sr-only" for="model-search">搜索模特</label>
      <div class="media-search">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
             aria-hidden="true"><circle cx="11" cy="11" r="7" /><path d="m20 20-3.5-3.5" /></svg>
        <input id="model-search" v-model="search" type="search"
               autocomplete="off" spellcheck="false" :disabled="searchBusy"
               aria-describedby="model-search-help"
               placeholder="筛选本地模特，Enter 从 PornHub 精确查找"
               @input="onSearchInput" @keydown.enter.prevent="submitModelSearch" />
      </div>
      <button class="btn" type="submit" :disabled="searchBusy || !search.trim()">
        {{ searchPhase === 'sync' ? '同步中…' : (searchBusy ? '查找中…' : '搜索') }}
      </button>
      <button class="btn secondary" type="button"
              :disabled="searchBusy || (!search && searchResultModelId === null)"
              @click="clearModelSearch">清除</button>
      <select v-model="sortBy" class="media-sort" aria-label="模特排序方式">
        <option value="rank">按名次</option>
        <option value="missing">缺失优先</option>
        <option value="progress">同步率优先</option>
        <option value="name">按名字</option>
      </select>
      <span class="media-filter-count">第 {{ pageIndex + 1 }} 页 · {{ models.length }} / {{ totalModels }} 位</span>
    </form>
    <p id="model-search-help" class="sr-only">
      输入时筛选本地模特；点击搜索或按 Enter 可从 PornHub 添加未入榜模特
    </p>
  </div>
  <p v-if="searchStatus" class="model-search-feedback success" role="status" aria-live="polite">
    {{ searchStatus }}
  </p>
  <p v-if="searchError" class="model-search-feedback error" role="alert">
    {{ searchError }}
  </p>

  <div v-if="initialLoading" class="media-grid" aria-busy="true">
    <div v-for="i in 12" :key="i" class="skeleton media-skeleton"></div>
  </div>

  <div v-else-if="models.length === 0 && !search.trim()" class="empty">
    还没有模特；在上方选择榜单来源并点击“爬取榜单”
  </div>
  <div v-else-if="searchBusy && models.length === 0" class="empty">
    正在查找「{{ search }}」…
  </div>
  <div v-else-if="models.length === 0" class="empty">
    本地没有匹配「{{ search }}」的模特；点击“搜索”可从 PornHub 精确查找
  </div>

  <div v-else ref="modelGrid" data-model-grid>
    <TransitionGroup name="card" tag="div" class="media-grid">
      <ModelCard v-for="model in models" :key="model.id"
                 :model="model" :syncing="Boolean(syncing[model.id])"
                 :active="detailId === model.id"
                 @open="openDetail" @download="download" />
    </TransitionGroup>
  </div>

  <nav v-if="!initialLoading && totalModels" class="pagination model-pagination"
       aria-label="模特列表分页">
    <button class="btn secondary" :disabled="pageLoading || pageIndex === 0"
            @click="previousPage">上一页</button>
    <span>第 {{ pageIndex + 1 }} 页 · 当前 {{ models.length }} / {{ totalModels }} 位</span>
    <button class="btn secondary" :disabled="pageLoading || !hasMore" @click="nextPage">
      {{ pageLoading ? '加载中…' : '下一页' }}
    </button>
  </nav>

  <ModelDrawer v-if="drawerModel" :model="drawerModel"
               :syncing="Boolean(syncing[drawerModel.id])"
               :index="drawerIndex" :total="models.length"
               :message="msg" :message-type="msgType"
               @close="closeDetail" @step="stepDetail"
               @download="download" @sync="syncOne" @remove="removeModel"
               @queued="onQueued" />
</template>
