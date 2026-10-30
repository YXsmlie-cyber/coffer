import { computed, onMounted, onUnmounted, ref, shallowRef } from 'vue'
import api, { subscribeWS, subscribeWSRefresh } from '../api'
import {
  applyTrayPatch, emptyTrayGroup, splitTrayGroup, trayGroupFromResponse, traySummary,
} from '../utils/taskTray'

// 全局任务概况：任务托盘与标签页标题/图标共用同一份状态。由 WebSocket 状态变化
// 驱动、节流读取，不做周期轮询。一次请求只取两类的进行中任务和合计计数；
// 标签页在后台时放宽节流，让标题仍能反映开始与完成，又不频繁请求。
const REFRESH_DELAY = 1500
const HIDDEN_REFRESH_DELAY = 10_000
// 进行中任务数受并发上限约束（下载 ≤64、上传 ≤32），一页即可全部取回。
const RUNNING_LIMIT = 100
// 托盘“✓ 完成 N”的停留时长；连续完成会顺延并累加。
const COMPLETED_FLASH_MS = 2500
// 托盘速度曲线：每 2 秒取一次当前总下载速度，保留最近 60 秒；只用本地计时器。
const SPEED_SAMPLE_MS = 2000
const SPEED_SAMPLES = 30

const state = shallowRef(emptyTrayGroup())
const loaded = ref(false)
const justCompleted = ref(0)
let flashTimer = null
const speedHistory = shallowRef([])
let sampleTimer = null
const groups = computed(() => splitTrayGroup(state.value))
const summary = computed(() => traySummary(groups.value))

let dirty = true
let users = 0
let timer = null
let running = null
let controller = null
let unsubscribe = null
let unsubscribeRefresh = null

function schedule(delay = document.hidden ? HIDDEN_REFRESH_DELAY : REFRESH_DELAY) {
  if (!dirty || timer !== null || running || !users) return
  timer = window.setTimeout(() => {
    timer = null
    void refresh()
  }, delay)
}

async function refresh() {
  if (!users || running || !dirty) return
  dirty = false
  const current = new AbortController()
  controller = current
  running = api.listTasks(undefined, {
    status: 'running', limit: RUNNING_LIMIT, offset: 0, include_counts: true,
  }, { signal: current.signal })
  try {
    const { data } = await running
    if (controller === current) {
      state.value = trayGroupFromResponse(data)
      loaded.value = true
    }
  } catch (error) {
    // 概况只是辅助信息；读取失败不打扰当前页面，下一次状态变化或重连时再补读。
    if (error?.code !== 'ERR_CANCELED') dirty = true
  } finally {
    running = null
    if (controller === current) controller = null
    schedule()
  }
}

function markDirty() {
  dirty = true
  schedule()
}

function flashCompleted() {
  justCompleted.value += 1
  window.clearTimeout(flashTimer)
  flashTimer = window.setTimeout(() => { justCompleted.value = 0 }, COMPLETED_FLASH_MS)
}

function onMessage(message) {
  if (message.type === 'network_health') {
    // 断流开始/结束即时反映到托盘与标题，不等下一次状态变化补读。
    state.value = { ...state.value, blocked: message.download_blocked || null }
    return
  }
  if (message.type !== 'task_update' || !message.task) return
  const result = applyTrayPatch(state.value, message.task)
  if (result.tracked) {
    state.value = result.group
    if (message.task.status === 'completed') flashCompleted()
  }
  if (result.refresh) markDirty()
}

function onVisibility() {
  // 回到前台时把后台积压的较长节流缩短为普通节流。
  if (document.hidden || !dirty || running) return
  window.clearTimeout(timer)
  timer = null
  schedule(REFRESH_DELAY)
}

function sampleSpeed() {
  // 没有下载在跑时清空曲线，下次开始从零画起。
  if (!summary.value.downloading) {
    if (speedHistory.value.length) speedHistory.value = []
    return
  }
  speedHistory.value = [...speedHistory.value, summary.value.speed].slice(-SPEED_SAMPLES)
}

function start() {
  sampleTimer = window.setInterval(sampleSpeed, SPEED_SAMPLE_MS)
  unsubscribe = subscribeWS(onMessage)
  unsubscribeRefresh = subscribeWSRefresh(markDirty)
  document.addEventListener('visibilitychange', onVisibility)
  dirty = true
  void refresh()
}

function stop() {
  window.clearTimeout(timer)
  window.clearInterval(sampleTimer)
  sampleTimer = null
  speedHistory.value = []
  window.clearTimeout(flashTimer)
  timer = null
  justCompleted.value = 0
  controller?.abort()
  unsubscribe?.()
  unsubscribeRefresh?.()
  unsubscribe = null
  unsubscribeRefresh = null
  document.removeEventListener('visibilitychange', onVisibility)
}

/** 组件挂载期间共享全局任务概况；最后一个使用者卸载时停止订阅。 */
export function useTaskPulse() {
  onMounted(() => {
    users += 1
    if (users === 1) start()
  })
  onUnmounted(() => {
    users -= 1
    if (users === 0) stop()
  })
  return { groups, loaded, summary, justCompleted, speedHistory }
}
