<script setup>
import { useRealtimeRefresh } from '../composables/useRealtimeRefresh'
import {
  computed, defineAsyncComponent, nextTick, onMounted, onUnmounted, ref,
} from 'vue'
import api, { subscribeWS } from '../api'
import TasteResultCard from '../components/TasteResultCard.vue'
import { apiErrorText } from '../utils/apiError'
import {
  hitRateBars, modelStatusCopy, pageSlice, ratingSummary, trendText, validationText,
} from '../utils/tasteView.js'

const LikedModelsWorkspace = defineAsyncComponent(
  () => import('../components/LikedModelsWorkspace.vue'))

defineOptions({ name: 'Taste' })

const modelPayload = ref(null)
const modelLoading = ref(true)
const modelAction = ref('')
const feedbackBusy = ref(new Set())
const modelError = ref('')
const announcement = ref('')
const page = ref(0)
const resultsAnchor = ref(null)
const likedWorkspaceRef = ref(null)

let pollTimer = null
let wsRefreshTimer = null
let unsubscribeWS = null
let mounted = false
let modelController = null
let modelGeneration = 0
let modelPromise = null
let modelDirty = false
// 每次进入页面最多自动生成一次；失败后由后端的冷却时间决定何时再自动尝试。
let autoStartTried = false

const modelRun = computed(() => modelPayload.value?.run || null)
const modelStatus = computed(() => modelPayload.value?.status || 'not_started')
const results = computed(() => modelPayload.value?.results || [])
const paged = computed(() => pageSlice(results.value, page.value))
const libraryMembers = computed(() => Number(modelPayload.value?.library?.members || 0))
const hiddenInLibrary = computed(() => Number(modelPayload.value?.results_hidden_in_library || 0))
const history = computed(() => modelPayload.value?.history || [])
const bars = computed(() => hitRateBars(history.value, modelPayload.value?.published_run_id))
const historySummary = computed(() => ratingSummary(history.value))
const historyTrend = computed(() => trendText(bars.value))
const formalMetrics = computed(() => modelRun.value?.metrics?.formal || null)
const validationCopy = computed(() => validationText(formalMetrics.value))
const statusCopy = computed(() => modelStatusCopy(modelStatus.value, formalMetrics.value))
const modelRunning = computed(() => [
  'validating', 'fetching_covers', 'indexing', 'scoring',
].includes(modelStatus.value))
const modelProgressPercent = computed(() => {
  const total = Number(modelRun.value?.progress_total || 0)
  const current = Number(modelRun.value?.progress_current || 0)
  return total > 0 ? Math.min(100, Math.round(current / total * 100)) : 0
})
const modelPhaseText = computed(() => ({
  validating: '正在读取你 115 收藏的特征',
  fetching_covers: '正在补齐候选的作品封面',
  indexing: '正在本机计算视觉特征',
  scoring: '正在挑选最像你收藏的人',
}[modelStatus.value] || '正在准备'))

function schedulePoll() {
  if (pollTimer) clearTimeout(pollTimer)
  pollTimer = null
  if (!mounted || !modelRunning.value) return
  pollTimer = setTimeout(async () => {
    await loadModelState(true)
  }, 2500)
}

function applyModelPayload(data) {
  modelPayload.value = data
  modelError.value = ''
  page.value = pageSlice(data?.results || [], page.value).page
  schedulePoll()
  if (data?.auto_start && !autoStartTried) {
    autoStartTried = true
    void startModelRun({ automatic: true })
  }
}

function modelRequestError(prefix, requestError) {
  if (requestError.response?.status === 404) {
    return '后端还没有加载推荐接口；请等当前媒体任务完成后安全重启服务。'
  }
  return `${prefix}：${apiErrorText(requestError)}`
}

async function loadModelState(silent = false) {
  modelDirty = true
  if (!mounted) return false
  if (modelPromise) return modelPromise
  modelPromise = (async () => {
    let loaded = false
    while (modelDirty && mounted) {
      modelDirty = false
      const generation = ++modelGeneration
      const controller = new AbortController()
      modelController = controller
      if (!silent) modelLoading.value = true
      try {
        const { data } = await api.tasteModelState({ signal: controller.signal })
        if (generation !== modelGeneration || !mounted) continue
        applyModelPayload(data)
        loaded = true
      } catch (requestError) {
        if (requestError.code !== 'ERR_CANCELED' && generation === modelGeneration) {
          modelError.value = modelRequestError('加载推荐失败', requestError)
          schedulePoll()
        }
      } finally {
        if (generation === modelGeneration && !silent) modelLoading.value = false
      }
    }
    return loaded
  })()
  try {
    return await modelPromise
  } finally {
    modelPromise = null
    modelController = null
    if (modelDirty && mounted) void loadModelState(true)
  }
}

async function reload() {
  modelDirty = true
  modelController?.abort()
  await loadModelState(false)
}

async function startModelRun({ automatic = false } = {}) {
  if (modelRunning.value || modelAction.value) return
  modelAction.value = 'start'
  modelError.value = ''
  if (!automatic) announcement.value = ''
  try {
    const { data } = await api.startTasteModelRun()
    applyModelPayload(data)
    if (!automatic) announcement.value = '已开始重新计算，可以先离开本页，完成后回来就能看到'
  } catch (requestError) {
    modelError.value = `无法开始生成：${apiErrorText(requestError)}`
  } finally {
    modelAction.value = ''
  }
}

async function nextPage() {
  page.value = (paged.value.page + 1) % paged.value.pageCount
  announcement.value = `已换到第 ${page.value + 1} 组`
  await nextTick()
  const reducedMotion = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
  resultsAnchor.value?.scrollIntoView({ behavior: reducedMotion ? 'auto' : 'smooth', block: 'start' })
}

async function recordFeedback(modelId, action) {
  const currentItem = results.value.find(result => result.model.id === modelId)
  if (currentItem?.feedback === action) return
  if (feedbackBusy.value.has(modelId)) return
  feedbackBusy.value = new Set(feedbackBusy.value).add(modelId)
  try {
    const { data } = await api.recordTasteResultFeedback({ model_id: modelId, action })
    if (currentItem) currentItem.feedback = data.feedback
    await likedWorkspaceRef.value?.reload()
    announcement.value = action === 'like'
      ? `已记录：${currentItem?.model?.name || ''} 符合，已加入下方“我的符合”`
      : `已记录：${currentItem?.model?.name || ''} 不符合，以后不会再推荐`
  } catch (requestError) {
    modelError.value = `评价没有保存：${apiErrorText(requestError)}`
  } finally {
    const next = new Set(feedbackBusy.value)
    next.delete(modelId)
    feedbackBusy.value = next
  }
}

function onWSMessage(message) {
  if (message.type !== 'taste_model_progress') return
  if (wsRefreshTimer) return
  wsRefreshTimer = setTimeout(async () => {
    wsRefreshTimer = null
    await loadModelState(true)
  }, 300)
}

onMounted(() => {
  mounted = true
  unsubscribeWS = subscribeWS(onWSMessage)
  void loadModelState(false)
})

onUnmounted(() => {
  mounted = false
  modelGeneration += 1
  modelController?.abort()
  if (pollTimer) clearTimeout(pollTimer)
  if (wsRefreshTimer) clearTimeout(wsRefreshTimer)
  if (unsubscribeWS) unsubscribeWS()
})

useRealtimeRefresh(reload)
</script>

<template>
  <section class="taste-page">
    <header class="page-title taste-title">
      <div>
        <span class="taste-eyebrow">审美推荐</span>
        <h2>审美探索</h2>
        <p class="sub">根据你 115 里收藏的模特，自动从还没收藏的人里挑出最像的。不需要比较，也不用逐个评价。</p>
      </div>
      <span class="taste-local-badge"
            title="推荐只在本机计算，不会自动下载；只有在“我的符合”里确认后才会加入下载">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
             stroke-width="2" aria-hidden="true">
          <rect x="5" y="10" width="14" height="10" rx="2" />
          <path d="M8 10V7a4 4 0 0 1 8 0v3" />
        </svg>
        仅本机计算 · 不会自动下载
      </span>
    </header>

    <div class="taste-live-region" aria-live="polite" aria-atomic="true">
      <p v-if="announcement" class="taste-announcement" role="status">{{ announcement }}</p>
    </div>

    <section class="taste-model-section" aria-labelledby="taste-model-title">
      <div v-if="modelLoading" class="taste-model-card taste-model-loading" aria-busy="true">
        <span class="taste-skeleton-line short"></span>
        <span class="taste-skeleton-line"></span>
        <span class="taste-skeleton-line"></span>
      </div>

      <template v-else>
        <div v-if="modelError" class="taste-error taste-model-error" role="alert">
          <span>{{ modelError }}</span>
          <button type="button" @click="reload">重新检查</button>
        </div>

        <section v-if="modelRunning" class="taste-model-card taste-model-running" aria-live="polite">
          <div class="taste-model-heading">
            <div>
              <span class="taste-model-kicker">
                正在根据你 115 里的 {{ libraryMembers }} 位收藏挑人
              </span>
              <h3 id="taste-model-title">{{ modelPhaseText }}</h3>
            </div>
            <strong class="taste-model-percent">{{ modelProgressPercent }}%</strong>
          </div>
          <p>通常一分钟内完成，可以先离开本页；中途出错不会产生不完整的推荐。</p>
          <div class="taste-model-track" role="progressbar"
               :aria-valuenow="modelRun?.progress_current || 0" aria-valuemin="0"
               :aria-valuemax="modelRun?.progress_total || 0"
               :aria-label="`${modelPhaseText} ${modelProgressPercent}%`">
            <span :style="{ '--progress': modelProgressPercent / 100 }"></span>
          </div>
          <div class="taste-model-progress-meta">
            <span>{{ modelRun?.progress_current || 0 }} / {{ modelRun?.progress_total || 0 }}</span>
            <span>只缓存图片，不下载视频</span>
          </div>
        </section>

        <section v-else-if="results.length" ref="resultsAnchor" class="taste-results-section">
          <div class="taste-results-header">
            <div>
              <span class="taste-model-kicker">
                根据你 115 里的 {{ libraryMembers }} 位收藏 · 第 {{ paged.page + 1 }} / {{ paged.pageCount }} 组
              </span>
              <h3 id="taste-model-title">最像你收藏的这些人</h3>
              <p>看中了就去收进 115，推荐会自动跟着你的收藏更新。“符合 / 不符合”可选：符合的进入下方“我的符合”，不符合的以后不再推荐。</p>
              <p v-if="hiddenInLibrary" class="taste-results-note">
                有 {{ hiddenInLibrary }} 位在推荐后已收进 115，已从这里隐藏。
              </p>
            </div>
            <div v-if="paged.pageCount > 1" class="taste-batch-action">
              <span>共 {{ results.length }} 位</span>
              <strong>第 {{ paged.page + 1 }} 组</strong>
              <small>每组 {{ paged.items.length }} 位，按相似度从高到低</small>
              <button type="button" class="btn" @click="nextPage">换一批</button>
            </div>
          </div>

          <section v-if="validationCopy || bars.length" class="taste-quality"
                   aria-labelledby="taste-quality-title">
            <div class="taste-quality-head">
              <div>
                <h4 id="taste-quality-title">推荐准不准</h4>
                <p>{{ validationCopy }}</p>
                <p v-if="historySummary">
                  {{ historySummary }}<template v-if="historyTrend">；{{ historyTrend }}</template>
                </p>
              </div>
            </div>
            <ol v-if="bars.length" class="taste-quality-bars" aria-label="各批推荐中符合的比例">
              <li v-for="bar in bars" :key="bar.key"
                  :title="`${bar.label}：${bar.rated} 位里 ${bar.liked} 位符合`">
                <span class="taste-quality-value">{{ Math.round(bar.rate * 100) }}%</span>
                <span class="taste-quality-bar" aria-hidden="true">
                  <span :style="{ '--rate': bar.rate }"></span>
                </span>
                <span class="taste-quality-label">{{ bar.label }}</span>
              </li>
            </ol>
            <details v-if="formalMetrics?.validated" class="taste-quality-details">
              <summary>检验细节</summary>
              <p>
                你过去评价过 {{ formalMetrics.labeled_models }} 位（{{ formalMetrics.positive_models }} 位符合、
                {{ formalMetrics.negative_models }} 位不符合）。检验时这些人不参与计算，只用来打分：
                区分度 AUC {{ Number(formalMetrics.auc).toFixed(2) }}（达标线 0.65）。
                这批从 {{ modelRun?.scored_count || 0 }} 位候选中挑选，已排除 115 已有与评价过的模特。
              </p>
            </details>
          </section>

          <div class="taste-results-grid">
            <TasteResultCard v-for="item in paged.items" :key="item.model.id"
                             :item="item" :busy="feedbackBusy.has(item.model.id)"
                             @feedback="recordFeedback" />
          </div>

          <div v-if="paged.pageCount > 1" class="taste-page-footer">
            <button type="button" class="btn secondary" @click="nextPage">
              换一批（第 {{ (paged.page + 1) % paged.pageCount + 1 }} / {{ paged.pageCount }} 组）
            </button>
          </div>
        </section>

        <section v-else class="taste-model-card"
                 :class="{ 'taste-model-failed': ['failed', 'interrupted', 'quality_failed'].includes(modelStatus) }">
          <div class="taste-model-heading">
            <div>
              <span class="taste-model-kicker">{{ statusCopy.kicker }}</span>
              <h3 id="taste-model-title">{{ statusCopy.title }}</h3>
            </div>
          </div>
          <p>{{ statusCopy.body }}</p>
          <p v-if="modelRun?.error && modelStatus !== 'quality_failed'" class="taste-model-error-copy">
            {{ modelRun.error }}
          </p>
          <div class="taste-model-actions">
            <button type="button" class="btn taste-model-primary"
                    :disabled="!modelPayload?.can_start || !!modelAction"
                    @click="startModelRun()">
              {{ modelAction ? '正在启动…' : statusCopy.action }}
            </button>
          </div>
        </section>
      </template>
    </section>

    <LikedModelsWorkspace ref="likedWorkspaceRef" />
  </section>
</template>
