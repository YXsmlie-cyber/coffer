<script setup>
import { computed, ref, watch } from 'vue'
import { resolveApiAssetUrl } from '../utils/assetUrl'

const props = defineProps({
  model: { type: Object, required: true },
  syncing: { type: Boolean, default: false },
})
const emit = defineEmits(['download', 'detail', 'sync', 'remove'])

const sourceRank = computed(() => Number(
  props.model.source_rank || (Number(props.model.source_order || 0) + 1),
))
const videos = computed(() => props.model.discovery_videos || [])
const activeVideoIndex = ref(0)
const draggingCover = ref(false)
const activeVideo = computed(() => videos.value[activeVideoIndex.value] || null)
const activeVideoKey = computed(() => {
  const video = activeVideo.value
  return `${video?.viewkey || video?.url || 'cover'}-${activeVideoIndex.value}`
})

let dragState = null
let suppressCoverClickUntil = 0

watch(() => props.model.id, () => {
  activeVideoIndex.value = 0
})

watch(() => videos.value.length, length => {
  if (!length || activeVideoIndex.value >= length) activeVideoIndex.value = 0
})

function showVideo(index) {
  const length = videos.value.length
  if (length < 2) return
  activeVideoIndex.value = (index + length) % length
}

function showPreviousVideo() {
  showVideo(activeVideoIndex.value - 1)
}

function showNextVideo() {
  showVideo(activeVideoIndex.value + 1)
}

function onCoverPointerDown(event) {
  if (videos.value.length < 2) return
  if (event.pointerType === 'mouse' && event.button !== 0) return

  dragState = {
    pointerId: event.pointerId,
    startX: event.clientX,
    startY: event.clientY,
  }
  draggingCover.value = true
  try {
    event.currentTarget.setPointerCapture?.(event.pointerId)
  } catch {
    // 某些嵌入式浏览器不允许捕获合成指针，滑动识别本身仍可继续。
  }
}

function onCoverPointerMove(event) {
  if (!dragState || dragState.pointerId !== event.pointerId) return
  const deltaX = event.clientX - dragState.startX
  const deltaY = event.clientY - dragState.startY
  if (Math.abs(deltaX) > 8 && Math.abs(deltaX) > Math.abs(deltaY)) {
    event.preventDefault()
  }
}

function finishCoverPointer(event, cancelled = false) {
  if (!dragState || dragState.pointerId !== event.pointerId) return

  const deltaX = event.clientX - dragState.startX
  const deltaY = event.clientY - dragState.startY
  const horizontalDistance = Math.abs(deltaX)
  const swipeThreshold = Math.max(36, Math.min(64, event.currentTarget.clientWidth * 0.14))
  const isHorizontalGesture = horizontalDistance > 8
    && horizontalDistance > Math.abs(deltaY)
  const isSwipe = !cancelled
    && horizontalDistance >= swipeThreshold
    && horizontalDistance > Math.abs(deltaY) * 1.15

  if (isHorizontalGesture) suppressCoverClickUntil = Date.now() + 400
  if (isSwipe) {
    if (deltaX < 0) showNextVideo()
    else showPreviousVideo()
  }

  if (event.currentTarget.hasPointerCapture?.(event.pointerId)) {
    try {
      event.currentTarget.releasePointerCapture(event.pointerId)
    } catch {
      // 指针可能已被浏览器释放，无需影响本次切换结果。
    }
  }
  dragState = null
  draggingCover.value = false
}

function onCoverClick(event) {
  if (Date.now() >= suppressCoverClickUntil) return
  event.preventDefault()
  event.stopPropagation()
}

function avatarSource() {
  return resolveApiAssetUrl(props.model.avatar_url)
}

function typeLabel(type) {
  return { model: '模特', pornstar: '明星', channel: '频道', user: '用户' }[type] || '上传者'
}

function progressPercent() {
  if (!props.model.total_videos) return 0
  return Math.round(props.model.cloud_videos / props.model.total_videos * 100)
}

function downloadableCount() {
  return props.model.downloadable_videos ?? props.model.missing_videos ?? 0
}

function downloadButtonText() {
  const count = downloadableCount()
  if (count > 0) return `下载 ${count}`
  if ((props.model.pending_upload_videos || 0) > 0) {
    return `待上传 ${props.model.pending_upload_videos}`
  }
  return '无可下载'
}

function downloadTitle() {
  const count = downloadableCount()
  if (count > 0) return `下载该上传者全部明确缺失的视频，共 ${count} 个`
  if (props.model.pending_upload_videos) return '视频已下载，正在等待上传'
  return '没有可下载视频'
}

function fmtDur(seconds) {
  const value = Number(seconds || 0)
  if (!value) return ''
  const h = Math.floor(value / 3600)
  const m = Math.floor((value % 3600) / 60)
  const s = value % 60
  return h
    ? `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
    : `${m}:${String(s).padStart(2, '0')}`
}
</script>

<template>
  <article class="discovery-card" :data-model-id="model.id">
    <div class="discovery-card-head">
      <span class="badge-rank" :class="{
        top1: sourceRank === 1, top2: sourceRank === 2, top3: sourceRank === 3,
      }">#{{ sourceRank }}</span>
      <span class="creator-type">{{ typeLabel(model.profile_type) }}</span>
      <span class="badge-sync" :class="{ synced: model.synced_at }">
        {{ syncing ? '同步中' : (model.synced_at ? '已同步' : '未同步') }}
      </span>
    </div>

    <div v-if="videos.length" class="discovery-covers">
      <div class="discovery-cover-stage"
           :class="{ 'is-draggable': videos.length > 1, 'is-dragging': draggingCover }"
           role="region" aria-roledescription="轮播图"
           :aria-label="`${model.name} 在当前来源出现的作品封面`"
           :aria-describedby="videos.length > 1 ? `discovery-cover-help-${model.id}` : undefined"
           @pointerdown="onCoverPointerDown"
           @pointermove="onCoverPointerMove"
           @pointerup="finishCoverPointer"
           @pointercancel="event => finishCoverPointer(event, true)"
           @keydown.left.prevent="showPreviousVideo"
           @keydown.right.prevent="showNextVideo">
        <Transition name="discovery-cover-fade">
          <a v-if="activeVideo" :key="activeVideoKey"
             :href="activeVideo.url || model.url" target="_blank" rel="noopener noreferrer"
             class="discovery-cover"
             :title="activeVideo.title || `打开 ${model.name} 的作品`"
             @click="onCoverClick" @dragstart.prevent>
            <img v-if="activeVideo.thumbnail" :src="activeVideo.thumbnail"
                 :alt="activeVideo.title || `${model.name} 的作品封面`"
                 loading="lazy" decoding="async" referrerpolicy="origin" draggable="false"
                 @error="$event.currentTarget.classList.add('is-broken')"
                 width="320" height="180" />
            <span :class="['discovery-cover-empty', {
                    'discovery-cover-fallback': activeVideo.thumbnail,
                  }]" aria-hidden="true">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7">
                <rect x="3" y="5" width="18" height="14" rx="2" />
                <path d="m10 9 5 3-5 3V9Z" />
              </svg>
            </span>
            <span v-if="fmtDur(activeVideo.duration)" class="discovery-duration">
              {{ fmtDur(activeVideo.duration) }}
            </span>
          </a>
        </Transition>

        <template v-if="videos.length > 1">
          <button type="button" class="discovery-cover-nav previous"
                  :aria-label="`${model.name}：上一张视频封面`" title="上一张视频封面"
                  @pointerdown.stop @click.stop="showPreviousVideo">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"
                 stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
              <path d="m15 18-6-6 6-6" />
            </svg>
          </button>
          <button type="button" class="discovery-cover-nav next"
                  :aria-label="`${model.name}：下一张视频封面`" title="下一张视频封面"
                  @pointerdown.stop @click.stop="showNextVideo">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"
                 stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
              <path d="m9 18 6-6-6-6" />
            </svg>
          </button>
          <span class="discovery-cover-count" aria-hidden="true">
            {{ activeVideoIndex + 1 }} / {{ videos.length }}
          </span>
        </template>
      </div>
      <span v-if="videos.length > 1" :id="`discovery-cover-help-${model.id}`"
            class="sr-only" aria-live="polite">
        当前第 {{ activeVideoIndex + 1 }} 张，共 {{ videos.length }} 张；可左右滑动或使用方向按钮切换
      </span>
    </div>
    <div v-else class="discovery-covers-empty">
      同步完成后显示该上传者的作品封面
    </div>

    <div class="discovery-creator">
      <a :href="model.url" target="_blank" rel="noopener noreferrer"
         class="discovery-avatar" :title="`在 PornHub 打开 ${model.name} 的主页`">
        <img v-if="avatarSource()" :src="avatarSource()" :alt="`${model.name} 的头像`"
             loading="lazy" decoding="async" width="52" height="52" />
        <span v-else aria-hidden="true">{{ typeLabel(model.profile_type).slice(0, 1) }}</span>
      </a>
      <div class="discovery-identity">
        <a :href="model.url" target="_blank" rel="noopener noreferrer"
           class="name" :title="model.name">{{ model.name }}</a>
        <span class="meta-line">
          @{{ model.profile_slug || model.ph_id }}
          <template v-if="model.discovery_count"> · 来源命中 {{ model.discovery_count }} 条</template>
        </span>
      </div>
    </div>

    <div class="stats">
      <div class="stat v-missing"><b>{{ model.missing_videos }}</b><label>缺失</label></div>
      <div v-if="model.maybe_videos" class="stat v-maybe"><b>{{ model.maybe_videos }}</b><label>疑似</label></div>
      <div class="stat v-exists"><b>{{ model.cloud_videos }}</b><label>已有</label></div>
      <div class="stat"><b>{{ model.total_videos }}</b><label>共计</label></div>
    </div>

    <div class="meter" :title="`115 已有 ${progressPercent()}%`">
      <div :style="{ '--progress': progressPercent() / 100 }"></div>
    </div>

    <div v-if="model.pending_upload_videos" class="pipeline-status pending-upload"
         :aria-label="`已下载待上传 ${model.pending_upload_videos} 个`">
      <span>已下载待上传</span><b>{{ model.pending_upload_videos }}</b>
    </div>

    <div class="actions">
      <button class="btn" :disabled="!model.synced_at || downloadableCount() === 0"
              :title="downloadTitle()" @click="emit('download', model, 'missing')">
        {{ downloadButtonText() }}
      </button>
      <button class="btn secondary" :disabled="!model.synced_at"
              title="查看完整视频列表并手动选择" @click="emit('detail', model)">选择</button>
      <button class="btn secondary" :disabled="syncing" @click="emit('sync', model.id)">
        {{ syncing ? '…' : '同步' }}
      </button>
      <button class="btn danger icon" :aria-label="`删除 ${model.name}`"
              title="删除此上传者" @click="emit('remove', model)">
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor"
             stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <path d="M3 6h18M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2m2 0v14a1 1 0 0 1-1 1H7a1 1 0 0 1-1-1V6" />
          <line x1="10" y1="11" x2="10" y2="17" />
          <line x1="14" y1="11" x2="14" y2="17" />
        </svg>
      </button>
    </div>
  </article>
</template>
