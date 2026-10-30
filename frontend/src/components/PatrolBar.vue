<script setup>
import { useRealtimeRefresh } from '../composables/useRealtimeRefresh'
import { computed, onActivated, onDeactivated, onUnmounted, ref } from 'vue'
import api, { subscribeWS } from '../api'
import { apiErrorText } from '../utils/apiError'
import { formatDateTime } from '../utils/formatters'

const emit = defineEmits(['feedback'])
const patrol = ref({
  enabled: false,
  running: false,
  scheduler_alive: false,
  run_now_pending: false,
  interval_hours: 6,
  last_run_at: null,
  next_run_at: null,
  last_heartbeat_at: null,
  last_result: null,
})
const patrolInterval = ref(6)
const actionBusy = ref(false)
let unsubscribe = null
let active = false
let statusController = null

function feedback(text, type = '') {
  emit('feedback', { text, type })
}

async function loadPatrol() {
  if (!active) return
  statusController?.abort()
  const controller = new AbortController()
  statusController = controller
  try {
    const { data } = await api.patrolStatus({ signal: controller.signal })
    if (!active || statusController !== controller) return
    patrol.value = data
    patrolInterval.value = data.interval_hours
  } catch (_) { /* 后端未就绪或页面失活时保持上一次状态 */ }
  finally {
    if (statusController === controller) statusController = null
  }
}

async function runAction(action, successText, failurePrefix) {
  if (actionBusy.value) return
  actionBusy.value = true
  try {
    const { data } = await action()
    patrol.value = data
    feedback(typeof successText === 'function' ? successText(data) : successText)
  } catch (error) {
    feedback(`${failurePrefix}：${apiErrorText(error)}`, 'err')
  } finally {
    actionBusy.value = false
    await loadPatrol()
  }
}

function togglePatrol() {
  const nextEnabled = !patrol.value.enabled
  return runAction(
    () => api.patrolToggle(nextEnabled),
    data => data.enabled ? '已开启自动巡查' : '已关闭自动巡查',
    '切换自动巡查失败',
  )
}

function applyInterval() {
  const hours = Number(patrolInterval.value)
  if (!(hours > 0)) {
    feedback('间隔需大于 0', 'err')
    return
  }
  return runAction(
    () => api.patrolConfig(hours),
    `巡查间隔已设为 ${hours} 小时`,
    '设置巡查间隔失败',
  )
}

function runPatrolNow() {
  return runAction(
    () => api.patrolRunNow(),
    data => data.enabled
      ? '已触发立即巡查，自动巡查保持开启'
      : '已触发一次性巡查（自动巡查仍为关闭）',
    '触发立即巡查失败',
  )
}

function formatTime(value) {
  return formatDateTime(value, value ? String(value) : '—')
}

const summary = computed(() => {
  const result = patrol.value.last_result
  if (!result) return ''
  if (result.skipped_reason) return `已跳过：${result.skipped_reason}`
  if (result.failure_reason) return `巡查失败：${result.failure_reason}`
  const parts = [
    `范围 ${result.bound_models ?? 0}/${result.total_models ?? 0}`,
    `扫描 ${result.scanned ?? 0}`,
    `缺失 ${result.missing ?? 0}`,
  ]
  if (result.newly_missing) parts.push(`新发现 ${result.newly_missing}`)
  if (result.queued_downloads) parts.push(`新建下载 ${result.queued_downloads}`)
  if (result.pipeline) parts.push(`流水线中 ${result.pipeline}`)
  if (result.repaired) parts.push(`自动修复 ${result.repaired}`)
  if (result.confirmation_pending) parts.push(`待复核 ${result.confirmation_pending}`)
  if (result.attention) parts.push(`需处理 ${result.attention}`)
  if (result.unmanaged) parts.push(`仅报告 ${result.unmanaged}`)
  if (result.source_unavailable) parts.push(`主页不可用 ${result.source_unavailable}`)
  if (result.errors) parts.push(`出错 ${result.errors}`)
  return parts.join(' · ')
})
const broken = computed(() => patrol.value.enabled && !patrol.value.scheduler_alive)

onActivated(() => {
  active = true
  void loadPatrol()
  unsubscribe ||= subscribeWS(message => {
    if (message.type === 'patrol_update' && message.patrol) {
      patrol.value = message.patrol
    }
  })
})
onDeactivated(() => {
  active = false
  statusController?.abort()
  unsubscribe?.()
  unsubscribe = null
})
onUnmounted(() => {
  active = false
  statusController?.abort()
  unsubscribe?.()
})

useRealtimeRefresh(loadPatrol)
</script>

<template>
  <section class="patrol-bar" :class="{ on: patrol.enabled, broken }"
           aria-label="自动巡查" :aria-busy="actionBusy">
    <span class="patrol-dot" :class="{ active: patrol.running }" aria-hidden="true"></span>
    <button class="btn" :class="patrol.enabled ? '' : 'secondary'"
            :disabled="actionBusy" @click="togglePatrol">
      自动巡查：{{ patrol.enabled ? '开' : '关' }}
    </button>
    <label class="patrol-label" for="patrol-interval">每</label>
    <input id="patrol-interval" v-model.number="patrolInterval" type="number"
           min="0.1" max="720" step="0.5" style="width:64px"
           title="巡查间隔（小时）" aria-label="巡查间隔小时数" />
    <span class="patrol-label">小时</span>
    <button class="btn secondary" :disabled="actionBusy" @click="applyInterval">应用</button>
    <button class="btn secondary"
            :disabled="actionBusy || patrol.running || patrol.run_now_pending"
            title="只立即执行一轮巡查；不会自动开启定时巡查"
            @click="runPatrolNow">
      {{ patrol.running ? '巡查中…' : (patrol.run_now_pending ? '已安排…' : '立即巡查一次') }}
    </button>
    <span class="spacer"></span>
    <span v-if="broken" class="hint patrol-error" role="alert">调度异常，请重启后端</span>
    <span v-if="summary" class="hint" role="status" aria-live="polite">{{ summary }}</span>
    <span class="hint">上次 {{ formatTime(patrol.last_run_at) }}</span>
    <span v-if="patrol.enabled && patrol.next_run_at" class="hint">
      · 下次 {{ formatTime(patrol.next_run_at) }}
    </span>
  </section>
</template>
