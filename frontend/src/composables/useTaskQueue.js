import { onMounted, onUnmounted, ref, shallowRef } from 'vue'
import api, { sortTasks, subscribeWS } from '../api'
import { useRealtimeRefresh } from './useRealtimeRefresh'
import { progressTime } from '../utils/progressTime'
import { TASK_VIEWS, taskMatchesView } from '../utils/taskViews.js'

const PAGE_SIZE = 200
const MAX_REPLAY_UPDATES = 2048
const EMPTY_COUNTS = {
  running: 0,
  queued: 0,
  paused: 0,
  completed: 0,
  failed: 0,
  failed_recoverable: 0,
  failed_manual: 0,
  failed_reconcile: 0,
  skipped: 0,
}

const DEFAULT_VIEW = 'active'
const SEARCH_DELAY = 300
// 刚完成的任务在统计条下停留的时长与最多同时显示的条数。
const COMPLETION_TOAST_MS = 4000
const MAX_COMPLETION_TOASTS = 3

function viewStorageKey(taskType) {
  return `ph115.taskView.${taskType}`
}

function readStoredView(taskType) {
  try {
    const value = window.localStorage.getItem(viewStorageKey(taskType))
    return value && value in TASK_VIEWS ? value : DEFAULT_VIEW
  } catch (_) {
    return DEFAULT_VIEW
  }
}

/** 下载与上传页共用的任务列表状态。 */
export function useTaskQueue(taskType) {
  const tasks = shallowRef([])
  const loading = ref(true)
  const refreshing = ref(false)
  const loadingMore = ref(false)
  const loadError = ref('')
  // 后端环境门禁（如缺 ffmpeg/ffprobe）暂缓新下载时的原因；无门禁为 null。
  const downloadBlocked = ref(null)
  // 115 整体拒绝本机上传时的暂停原因（仅上传页）；闸门开关时由推送即时更新。
  const uploadBlocked = ref(null)
  // 近一小时共享链路断流统计（仅下载页），断流开始/结束时由推送即时更新。
  const network = ref(null)
  const total = ref(0)
  // 当前筛选视图的命中总数；total 仍是全部任务数，供“删除全部”确认使用。
  const matched = ref(0)
  const counts = ref({ ...EMPTY_COUNTS })
  const selectedIds = ref([])
  const tableRef = ref(null)
  const nextCursor = ref(null)
  const listGeneration = ref(0)
  const view = ref(readStoredView(taskType))
  const search = ref('')
  // 今日完成小结（队列清空时展示）与刚完成提示；只由已有读取和推送驱动。
  const today = ref(null)
  const recentCompletions = ref([])
  const completedBump = ref(0)
  let searchTimer = null
  const completionTimers = new Set()

  function noteCompletion(task) {
    completedBump.value += 1
    recentCompletions.value = [
      { id: task.id, title: task.title || task.viewkey || `任务 ${task.id}` },
      ...recentCompletions.value.filter(item => item.id !== task.id),
    ].slice(0, MAX_COMPLETION_TOASTS)
    const timer = window.setTimeout(() => {
      completionTimers.delete(timer)
      recentCompletions.value = recentCompletions.value.filter(item => item.id !== task.id)
    }, COMPLETION_TOAST_MS)
    completionTimers.add(timer)
  }

  function filterParams() {
    const params = { ...TASK_VIEWS[view.value] }
    const keyword = search.value.trim()
    if (keyword) params.q = keyword
    return params
  }

  // 筛选变化时作废进行中的旧条件请求，避免先闪出旧视图再被替换。
  function reloadForFilter() {
    loadController?.abort()
    loadMoreController?.abort()
    void load()
  }

  function setView(next) {
    const value = next in TASK_VIEWS ? next : DEFAULT_VIEW
    if (value === view.value) return
    view.value = value
    try { window.localStorage.setItem(viewStorageKey(taskType), value) } catch (_) {}
    reloadForFilter()
  }

  function setSearch(text) {
    search.value = String(text || '')
    if (searchTimer !== null) window.clearTimeout(searchTimer)
    searchTimer = window.setTimeout(() => {
      searchTimer = null
      reloadForFilter()
    }, SEARCH_DELAY)
  }

  function keepVisible(list) {
    return view.value === 'all' ? list : list.filter(task => taskMatchesView(task, view.value))
  }

  let loadPromise = null
  let loadDirty = true
  let loadController = null
  let loadGeneration = 0
  let loadMoreController = null
  let mounted = false
  let refreshTimer = null
  let unsubscribe = null
  let updateFrame = null
  let wsSequence = 0
  const taskIndex = new Map()
  const pendingUpdates = new Map()
  const recentUpdates = []
  const liveTelemetry = new Map()
  useRealtimeRefresh(load)

  function rebuildTaskIndex() {
    taskIndex.clear()
    tasks.value.forEach((task, index) => taskIndex.set(task.id, index))
  }

  function replaceTaskList(next) {
    tasks.value = next
    rebuildTaskIndex()
    listGeneration.value += 1
  }

  function mergeUpdatesSince(next, sinceSequence, nextCounts = null) {
    const index = new Map(next.map((task, position) => [task.id, position]))
    let copied = false
    let needsSort = false
    for (const { sequence, patch } of recentUpdates) {
      if (sequence <= sinceSequence) continue
      const position = index.get(patch.id)
      if (position === undefined) continue
      if (!copied) { next = [...next]; copied = true }
      const previous = next[position]
      const updated = { ...previous, ...patch }
      next[position] = updated
      if (patch.status && previous.status !== updated.status) {
        needsSort = true
        if (nextCounts) {
          nextCounts[previous.status] = Math.max(
            0, (nextCounts[previous.status] || 0) - 1)
          nextCounts[updated.status] = (nextCounts[updated.status] || 0) + 1
        }
      }
    }
    // SQLite 字节快照最多落后约 5 秒；刷新不能覆盖已收到的更新遥测。
    next = next.map(task => {
      const live = liveTelemetry.get(task.id)
      if (!live || task.status !== 'running' || live.status !== task.status) return task
      const latestTime = progressTime(live.last_progress_at)
      const snapshotTime = progressTime(task.last_progress_at)
      if (!Number.isFinite(latestTime)
          || (Number.isFinite(snapshotTime) && latestTime <= snapshotTime)) return task
      return { ...task, ...live.fields }
    })
    return needsSort ? keepVisible(sortTasks(next)) : next
  }

  async function load() {
    loadDirty = true
    if (!mounted) return false
    if (loadPromise) return loadPromise
    loadPromise = (async () => {
      let loaded = false
      while (loadDirty && mounted) {
        loadDirty = false
        loadMoreController?.abort()
        const generation = ++loadGeneration
        const controller = new AbortController()
        const requestSequence = wsSequence
        loadController = controller
        const initialLoad = tasks.value.length === 0
        loading.value = initialLoad
        refreshing.value = !initialLoad
        loadError.value = ''
        try {
          const { data } = await api.listTasks(taskType, {
            limit: PAGE_SIZE,
            offset: 0,
            include_counts: true,
            ...filterParams(),
          }, { signal: controller.signal })
          if (generation !== loadGeneration || !mounted) continue
          const nextCounts = { ...EMPTY_COUNTS, ...data.counts }
          const appliedSequence = wsSequence
          const nextTasks = mergeUpdatesSince(
            sortTasks(data.tasks), requestSequence, nextCounts)
          replaceTaskList(nextTasks)
          nextCursor.value = data.next_cursor ?? null
          counts.value = nextCounts
          downloadBlocked.value = data.download_blocked || null
          uploadBlocked.value = data.upload_blocked || null
          network.value = data.network || null
          total.value = data.total ?? data.tasks.length
          matched.value = data.matched ?? total.value
          today.value = data.today || null
          while (recentUpdates[0]?.sequence <= appliedSequence) recentUpdates.shift()
          loaded = true
        } catch (error) {
          if (error.code !== 'ERR_CANCELED' && generation === loadGeneration) {
            loadError.value = '任务加载失败，请点击刷新重试'
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
      if (loadDirty && mounted) void load()
    }
  }

  async function loadMore() {
    if (!mounted || loadPromise || loadingMore.value
        || !nextCursor.value || tasks.value.length >= matched.value) return
    loadingMore.value = true
    const controller = new AbortController()
    const requestSequence = wsSequence
    const generation = loadGeneration
    loadMoreController = controller
    try {
      const { data } = await api.listTasks(taskType, {
        limit: PAGE_SIZE,
        cursor: nextCursor.value,
        ...filterParams(),
      }, { signal: controller.signal })
      if (loadMoreController !== controller || generation !== loadGeneration || !mounted) return
      const appliedSequence = wsSequence
      const known = new Set(tasks.value.map(task => task.id))
      replaceTaskList(mergeUpdatesSince(sortTasks([
        ...tasks.value,
        ...data.tasks.filter(task => !known.has(task.id)),
      ]), requestSequence))
      nextCursor.value = data.next_cursor ?? null
      while (recentUpdates[0]?.sequence <= appliedSequence) recentUpdates.shift()
    } catch (error) {
      if (error.code !== 'ERR_CANCELED') throw error
    } finally {
      if (loadMoreController === controller) {
        loadMoreController = null
        loadingMore.value = false
      }
    }
  }

  function scheduleRefresh() {
    loadDirty = true
    if (loadPromise) return
    if (refreshTimer !== null) return
    refreshTimer = window.setTimeout(() => {
      refreshTimer = null
      void load()
    }, 300)
  }

  function onTaskUpdate(message) {
    if (message.type === 'upload_gate' && taskType === 'upload') {
      uploadBlocked.value = message.upload_blocked || null
      return
    }
    if (message.type === 'network_health' && taskType === 'download') {
      downloadBlocked.value = message.download_blocked || null
      network.value = message.health || null
      return
    }
    if (message.type !== 'task_update' || message.task.task_type !== taskType) return
    wsSequence += 1
    recentUpdates.push({ sequence: wsSequence, patch: message.task })
    if (recentUpdates.length > MAX_REPLAY_UPDATES) {
      // 长时间读失败时不能无限保存重放日志。丢弃历史前作废旧快照，
      // 重新读取已提交的状态；随后到达的消息仍按新请求的序号合并。
      loadGeneration += 1
      loadController?.abort()
      loadMoreController?.abort()
      recentUpdates.length = 0
      scheduleRefresh()
    }
    const current = tasks.value[taskIndex.get(message.task.id)]
    if (message.task.last_progress_at) {
      const previous = liveTelemetry.get(message.task.id)
      const fields = { ...previous?.fields }
      for (const key of ['progress', 'speed', 'speed_bps', 'downloaded_bytes', 'total_bytes', 'last_progress_at']) {
        if (key in message.task) fields[key] = message.task[key]
      }
      liveTelemetry.delete(message.task.id)
      liveTelemetry.set(message.task.id, {
        status: message.task.status || current?.status,
        last_progress_at: message.task.last_progress_at, fields,
      })
      if (liveTelemetry.size > 1000) liveTelemetry.delete(liveTelemetry.keys().next().value)
    }
    if (message.task.status && message.task.status !== 'running') liveTelemetry.delete(message.task.id)
    if (!loadPromise && !loadMoreController && recentUpdates.length > 1000) {
      recentUpdates.shift()
    }
    if (!taskIndex.has(message.task.id)) {
      // 筛选视图下，未列出任务的纯遥测不会改变本视图，只有进入本视图的状态变化
      // 才需要补读；否则“完成”等视图会被运行任务的每秒进度触发刷新风暴。
      const filtered = view.value !== 'all' || search.value.trim()
      if (filtered && (!message.task.status || !taskMatchesView(message.task, view.value))) return
      // 一批后台任务可能同时更新；合并成一次列表刷新，避免请求风暴。
      scheduleRefresh()
      return
    }

    const previous = pendingUpdates.get(message.task.id) || {}
    pendingUpdates.set(message.task.id, { ...previous, ...message.task })
    if (updateFrame === null) updateFrame = window.requestAnimationFrame(flushTaskUpdates)
  }

  function flushTaskUpdates() {
    updateFrame = null
    if (!pendingUpdates.size) return
    let next = tasks.value
    let copied = false
    let needsSort = false
    let refreshFailureGroups = false

    for (const [id, patch] of pendingUpdates) {
      const index = taskIndex.get(id)
      if (index === undefined) continue
      if (!copied) {
        next = [...next]
        copied = true
      }
      const previous = next[index]
      const oldStatus = previous.status
      const updated = { ...previous, ...patch }
      next[index] = updated
      const newStatus = updated.status
      if (patch.status && oldStatus !== newStatus) {
        counts.value[oldStatus] = Math.max(0, (counts.value[oldStatus] || 0) - 1)
        counts.value[newStatus] = (counts.value[newStatus] || 0) + 1
        needsSort = true
        if (oldStatus === 'failed' || newStatus === 'failed') refreshFailureGroups = true
        if (newStatus === 'completed' && oldStatus === 'running') noteCompletion(updated)
      }
    }
    pendingUpdates.clear()
    if (!copied) return
    tasks.value = needsSort ? keepVisible(sortTasks(next)) : next
    if (needsSort) rebuildTaskIndex()
    // 任务离开或进入当前视图会改变命中数；节流补读一次以校准。
    if (refreshFailureGroups || (needsSort && view.value !== 'all')) scheduleRefresh()
  }

  async function runAndRefresh(request) {
    const { data } = await request()
    await load()
    return data
  }

  const startAll = () => runAndRefresh(() => api.startAll(taskType))
  const pauseAll = () => runAndRefresh(() => api.pauseAll(taskType))
  const resumeAll = () => runAndRefresh(() => api.resumeAll(taskType))
  const retryFailed = () => runAndRefresh(() => api.retryFailed(taskType))

  async function deleteSelected() {
    const { data } = await api.deleteTasks([...selectedIds.value])
    tableRef.value?.clearSelection()
    selectedIds.value = []
    await load()
    return data
  }

  async function deleteAll() {
    const { data } = await api.deleteAllTasks(taskType)
    tableRef.value?.clearSelection()
    selectedIds.value = []
    await load()
    return data
  }

  onMounted(() => {
    mounted = true
    void load()
    unsubscribe = subscribeWS(onTaskUpdate)
  })
  onUnmounted(() => {
    mounted = false
    loadGeneration += 1
    loadController?.abort()
    loadMoreController?.abort()
    if (refreshTimer !== null) window.clearTimeout(refreshTimer)
    if (searchTimer !== null) window.clearTimeout(searchTimer)
    completionTimers.forEach(timer => window.clearTimeout(timer))
    completionTimers.clear()
    if (updateFrame !== null) window.cancelAnimationFrame(updateFrame)
    pendingUpdates.clear()
    recentUpdates.length = 0
    liveTelemetry.clear()
    unsubscribe?.()
  })

  return {
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
  }
}
