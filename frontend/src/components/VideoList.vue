<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import api from '../api'
import { apiErrorText } from '../utils/apiError'
import { confirm } from '../composables/useDialog'

// 弹窗与详情抽屉共用的视频筛选、虚拟列表和“下载已选”；外壳负责焦点与关闭。
const props = defineProps({
  model: { type: Object, required: true },
  confirmBeforeQueue: { type: Boolean, default: false },
  closable: { type: Boolean, default: false },
})
const emit = defineEmits(['close', 'queued'])

const videos = ref([])
const loading = ref(true)
const selected = ref(new Set())
const filter = ref('downloadable')   // downloadable(missing+maybe) / missing / maybe / exists / all
const submitting = ref(false)
const loadError = ref('')
const submitError = ref('')
const submitStatus = ref('')
const viewport = ref(null)
const scrollTop = ref(0)
const viewportHeight = ref(420)
const ROW_HEIGHT = 58
const HEADER_HEIGHT = 41
const BUFFER_ROWS = 6
let resizeObserver = null
let loadController = null

const statusText = { missing: '缺失', maybe: '可能已有', exists: '已有' }

function fmtDur(sec) {
  if (!sec) return ''
  const h = Math.floor(sec / 3600)
  const m = Math.floor((sec % 3600) / 60)
  const s = sec % 60
  return h > 0
    ? `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
    : `${m}:${String(s).padStart(2, '0')}`
}

async function load() {
  loadController?.abort()
  const controller = new AbortController()
  loadController = controller
  loading.value = true
  loadError.value = ''
  try {
    const { data } = await api.modelVideos(props.model.id, { signal: controller.signal })
    // 排序：missing > maybe > exists
    const order = { missing: 0, maybe: 1, exists: 2 }
    videos.value = data.videos.sort(
      (a, b) => (order[a.dedup_status] ?? 9) - (order[b.dedup_status] ?? 9))
  } catch (e) {
    if (e.code !== 'ERR_CANCELED') loadError.value = apiErrorText(e)
  } finally {
    if (loadController === controller) {
      loading.value = false
      loadController = null
    }
  }
}

const videoByKey = computed(() => new Map(
  videos.value.map(video => [video.viewkey, video])))

const filtered = computed(() => {
  if (filter.value === 'all') return videos.value
  if (filter.value === 'downloadable')
    return videos.value.filter(v => v.dedup_status === 'missing' || v.dedup_status === 'maybe')
  return videos.value.filter(v => v.dedup_status === filter.value)
})

const counts = computed(() => {
  const c = { missing: 0, maybe: 0, exists: 0 }
  for (const v of videos.value) c[v.dedup_status] = (c[v.dedup_status] || 0) + 1
  return c
})

function toggle(vk) {
  const video = videoByKey.value.get(vk)
  if (!video || video.dedup_status === 'exists') return
  if (selected.value.has(vk)) selected.value.delete(vk)
  else selected.value.add(vk)
  selected.value = new Set(selected.value)
  submitStatus.value = ''
}

const selectableFiltered = computed(() => filtered.value.filter(
  video => video.dedup_status !== 'exists'))
const allFilteredChecked = computed(() =>
  selectableFiltered.value.length > 0
  && selectableFiltered.value.every(v => selected.value.has(v.viewkey)))
const someFilteredChecked = computed(() => {
  const checked = selectableFiltered.value.filter(
    video => selected.value.has(video.viewkey)).length
  return checked > 0 && checked < selectableFiltered.value.length
})

function toggleAll() {
  if (allFilteredChecked.value) {
    for (const v of selectableFiltered.value) selected.value.delete(v.viewkey)
  } else {
    for (const v of selectableFiltered.value) selected.value.add(v.viewkey)
  }
  selected.value = new Set(selected.value)
  submitStatus.value = ''
}

function measureViewport() {
  viewportHeight.value = viewport.value?.clientHeight || 420
}

function observeViewport(element) {
  resizeObserver?.disconnect()
  resizeObserver = null
  if (!element) return
  measureViewport()
  if (window.ResizeObserver) {
    resizeObserver = new ResizeObserver(measureViewport)
    resizeObserver.observe(element)
  }
}

function onScroll() {
  scrollTop.value = viewport.value?.scrollTop || 0
}

const virtualStart = computed(() => Math.max(
  0,
  Math.floor(Math.max(0, scrollTop.value - HEADER_HEIGHT) / ROW_HEIGHT) - BUFFER_ROWS,
))
const virtualCount = computed(() => (
  Math.ceil(viewportHeight.value / ROW_HEIGHT) + BUFFER_ROWS * 2
))
const virtualEnd = computed(() => Math.min(
  filtered.value.length,
  virtualStart.value + virtualCount.value,
))
const visibleFiltered = computed(() => filtered.value.slice(
  virtualStart.value, virtualEnd.value))
const virtualOffset = computed(() => virtualStart.value * ROW_HEIGHT)
const virtualHeight = computed(() => filtered.value.length * ROW_HEIGHT)

watch(filter, async () => {
  scrollTop.value = 0
  await nextTick()
  if (viewport.value) viewport.value.scrollTop = 0
})
watch(viewport, observeViewport, { flush: 'post' })

async function downloadSelected() {
  const viewkeys = [...selected.value]
  if (!viewkeys.length) return
  if (props.confirmBeforeQueue) {
    const chosen = videos.value.filter(video => selected.value.has(video.viewkey))
    const missing = chosen.filter(video => video.dedup_status === 'missing').length
    const maybe = chosen.filter(video => video.dedup_status === 'maybe').length
    const maybeText = maybe ? `，其中 ${maybe} 个是“可能已有”，你已明确选择继续` : ''
    const accepted = await confirm({
      title: `确认下载 ${chosen.length} 个视频`,
      message: `将为“${props.model.name}”创建 ${chosen.length} 个下载任务（${missing} 个明确缺失${maybeText}）。下载完成后会按现有规则自动创建上传任务，并在上传前再次去重。`,
      confirmText: '加入下载队列',
    })
    if (!accepted) return
  }
  submitting.value = true
  submitError.value = ''
  submitStatus.value = ''
  try {
    const { data } = await api.addDownload(props.model.id, 'selected', viewkeys)
    selected.value = new Set()
    submitStatus.value = `已加入 ${data.added} 个下载任务`
    emit('queued', data.added)
  } catch (e) {
    submitError.value = apiErrorText(e)
  } finally {
    submitting.value = false
  }
}

onMounted(() => {
  void load()
  if (!window.ResizeObserver) {
    window.addEventListener('resize', measureViewport)
  }
})
onBeforeUnmount(() => {
  loadController?.abort()
  resizeObserver?.disconnect()
  window.removeEventListener('resize', measureViewport)
})
</script>

<template>
  <div class="vlm">
    <div class="vlm-filters">
      <button :class="{ active: filter === 'downloadable' }"
              :aria-pressed="filter === 'downloadable'" @click="filter = 'downloadable'">
        可下载 {{ counts.missing + counts.maybe }}
      </button>
      <button :class="{ active: filter === 'missing' }"
              :aria-pressed="filter === 'missing'" @click="filter = 'missing'">
        缺失 {{ counts.missing }}
      </button>
      <button :class="{ active: filter === 'maybe' }"
              :aria-pressed="filter === 'maybe'" @click="filter = 'maybe'">
        可能已有 {{ counts.maybe }}
      </button>
      <button :class="{ active: filter === 'exists' }"
              :aria-pressed="filter === 'exists'" @click="filter = 'exists'">
        已有 {{ counts.exists }}
      </button>
      <button :class="{ active: filter === 'all' }"
              :aria-pressed="filter === 'all'" @click="filter = 'all'">
        全部 {{ videos.length }}
      </button>
    </div>

    <div v-if="loading" class="empty">加载中</div>
    <div v-else-if="loadError" class="empty form-error" role="alert">
      加载失败：{{ loadError }}
      <button class="btn ghost sm" @click="load">重试</button>
    </div>
    <div v-else-if="filtered.length === 0" class="empty">无视频</div>

    <div v-else ref="viewport" class="vlm-list" @scroll.passive="onScroll">
      <div class="vlm-head">
        <input type="checkbox" :checked="allFilteredChecked"
               :indeterminate.prop="someFilteredChecked"
               aria-label="选择当前筛选的全部视频" @change="toggleAll" />
        <span>标题</span>
        <span>时长</span>
        <span>状态</span>
      </div>
      <div class="vlm-virtual-spacer" :style="{ height: `${virtualHeight}px` }">
        <div class="vlm-virtual-window" :style="{ transform: `translateY(${virtualOffset}px)` }">
          <label v-for="v in visibleFiltered" :key="v.viewkey"
                 class="vlm-row" :class="v.dedup_status">
            <input type="checkbox" :checked="selected.has(v.viewkey)"
                   :disabled="v.dedup_status === 'exists'"
                   :aria-label="v.dedup_status === 'exists'
                     ? `${v.title} 已在 115，不能重复下载`
                     : `选择 ${v.title}`"
                   @change="toggle(v.viewkey)" />
            <span class="vlm-title" :title="v.title">
              {{ v.title }}
              <em v-if="v.dedup_status === 'maybe' && v.matched_cloud_name"
                  :title="'疑似匹配：' + v.matched_cloud_name">≈ {{ v.matched_cloud_name }}</em>
            </span>
            <span class="vlm-dur">{{ fmtDur(v.duration) }}</span>
            <span class="vlm-status" :class="v.dedup_status">{{ statusText[v.dedup_status] }}</span>
          </label>
        </div>
      </div>
    </div>

    <div class="vlm-footer">
      <span v-if="submitError" class="form-error" role="alert">{{ submitError }}</span>
      <span v-else-if="submitStatus" class="hint vlm-submitted" role="status">{{ submitStatus }}</span>
      <span v-else class="hint">已选 {{ selected.size }} 个</span>
      <span class="spacer"></span>
      <button v-if="closable" class="btn ghost sm" @click="emit('close')">关闭</button>
      <button class="btn" :disabled="!selected.size || submitting" @click="downloadSelected">
        {{ submitting ? '添加中' : `下载已选 (${selected.size})` }}
      </button>
    </div>
  </div>
</template>
