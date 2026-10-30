<script setup>
import { computed, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { useTaskPulse } from '../composables/useTaskPulse'
import { formatDuration, formatSpeed } from '../utils/formatters'
import { trayPhaseText } from '../utils/taskTray'
import { sparklinePoints } from '../utils/sparkline.js'

// 浏览页右下角的任务托盘，只负责展示 useTaskPulse 的全局概况；
// 下载/上传页已有完整统计，在那里隐藏。
const route = useRoute()
const { groups, loaded, summary, justCompleted, speedHistory } = useTaskPulse()
// 至少两个采样点才画曲线；断流时会掉到底部，直观看出速度变化。
const sparkline = computed(() => (
  speedHistory.value.length >= 2 ? sparklinePoints(speedHistory.value) : ''))
const expanded = ref(false)
// 首次导航尚未落定（懒加载页面）时先不显示。
const suppressed = computed(() => (
  !route.name || route.name === 'downloads' || route.name === 'uploads'))
// 最后一个任务完成后托盘会变空闲；先让“✓ 完成”停留片刻再隐藏。
const visible = computed(() => loaded.value && !suppressed.value
  && (!summary.value.idle || justCompleted.value > 0))
const runningGroups = computed(() => [
  { type: 'download', title: '下载中', tasks: groups.value.download.running },
  { type: 'upload', title: '上传中', tasks: groups.value.upload.running },
].filter(group => group.tasks.length))
const pillLabel = computed(() => {
  const parts = []
  if (summary.value.downloading) parts.push(`下载中 ${summary.value.downloading}`)
  if (summary.value.uploading) parts.push(`上传中 ${summary.value.uploading}`)
  if (justCompleted.value) parts.push(`刚完成 ${justCompleted.value}`)
  if (summary.value.queued) parts.push(`排队 ${summary.value.queued}`)
  if (summary.value.failed) parts.push(`失败 ${summary.value.failed}`)
  if (summary.value.blocked) parts.push(`新下载已暂缓，${blockedLabel.value}`)
  return `任务托盘：${parts.join('，')}`
})

const blockedLabel = computed(() => ({
  network_outage: '网络中断',
  rate_limited: 'CDN 限流冷却中',
  download_window: '等待下载时段',
}[summary.value.blockedCode] || '缺少 ffmpeg'))

watch(suppressed, value => { if (value) expanded.value = false })
watch(() => route.fullPath, () => { expanded.value = false })
</script>

<template>
  <div v-if="visible" class="task-tray" :class="{ expanded }"
       @keydown.esc="expanded = false">
    <section v-if="expanded" id="task-tray-panel" class="task-tray-panel" aria-label="任务进度">
      <div v-for="group in runningGroups" :key="group.type" class="tray-group">
        <h4>
          {{ group.title }} · {{ group.tasks.length }}
          <span v-if="group.type === 'download' && summary.eta" class="tray-eta"
                title="按当前速度估算，最慢的一个完成所需时间">
            全部{{ formatDuration(summary.eta) }}
          </span>
        </h4>
        <svg v-if="group.type === 'download' && sparkline" class="tray-sparkline"
             viewBox="0 0 120 24" preserveAspectRatio="none" aria-hidden="true">
          <polyline :points="sparkline" fill="none" stroke="currentColor"
                    stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round"
                    vector-effect="non-scaling-stroke" />
        </svg>
        <ul>
          <li v-for="task in group.tasks" :key="task.id" class="tray-task">
            <div class="tray-task-head">
              <span class="tray-task-title" :title="task.title">{{ task.title }}</span>
              <span class="tray-task-pct">{{ Math.round(Number(task.progress) || 0) }}%</span>
            </div>
            <div class="tray-task-bar" aria-hidden="true">
              <span :style="{ '--progress': Math.min(100, Number(task.progress) || 0) / 100 }"></span>
            </div>
            <small>
              {{ task.model_name }}
              <template v-if="trayPhaseText(task) || formatSpeed(task.speed_bps)">
                · {{ trayPhaseText(task) || formatSpeed(task.speed_bps) }}
              </template>
            </small>
          </li>
        </ul>
      </div>
      <p v-if="groups.download.blocked" class="tray-empty tray-blocked" role="status">
        新下载已暂缓：{{ groups.download.blocked.message }}
      </p>
      <p v-if="!runningGroups.length" class="tray-empty">当前没有进行中的任务</p>
      <footer class="tray-footer">
        <span>排队 {{ summary.queued }} · 失败 {{ summary.failed }}</span>
        <span class="tray-links">
          <router-link to="/downloads">下载队列</router-link>
          <router-link to="/uploads">上传队列</router-link>
        </span>
      </footer>
    </section>

    <button type="button" class="task-tray-pill" :aria-expanded="expanded"
            aria-controls="task-tray-panel" :aria-label="pillLabel"
            @click="expanded = !expanded">
      <span v-if="summary.downloading" class="tray-chip downloading">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"
             aria-hidden="true"><path d="M12 5v12m0 0-5-5m5 5 5-5" /></svg>
        {{ summary.downloading }}
        <b v-if="summary.speed">{{ formatSpeed(summary.speed) }}</b>
      </span>
      <span v-if="summary.uploading" class="tray-chip uploading">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"
             aria-hidden="true"><path d="M12 19V7m0 0-5 5m5-5 5 5" /></svg>
        {{ summary.uploading }}
      </span>
      <Transition name="fade">
        <span v-if="justCompleted" class="tray-chip done">✓ 完成 {{ justCompleted }}</span>
      </Transition>
      <span v-if="summary.queued" class="tray-chip">排队 {{ summary.queued }}</span>
      <span v-if="summary.failed" class="tray-chip failed">失败 {{ summary.failed }}</span>
      <span v-if="summary.blocked" class="tray-chip"
            :class="{ failed: summary.blockedCode !== 'download_window' }">{{ blockedLabel }}</span>
      <svg class="tray-caret" viewBox="0 0 24 24" fill="none" stroke="currentColor"
           stroke-width="2" aria-hidden="true"><path d="m6 15 6-6 6 6" /></svg>
    </button>
  </div>
</template>
