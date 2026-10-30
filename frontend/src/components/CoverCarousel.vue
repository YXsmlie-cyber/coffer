<script setup>
import { computed, onBeforeUnmount, onDeactivated, ref, watch } from 'vue'
import { resolveApiAssetUrl } from '../utils/assetUrl'
import { useModalFocus } from '../composables/useModalFocus'
import { isTopModal } from '../utils/modalStack'

const props = defineProps({
  videos: { type: Array, default: () => [] },
  name: { type: String, default: '模特' },
  fallbackUrl: { type: String, default: '' },
  identity: { type: [String, Number], default: '' },
})

const index = ref(0)
const dragging = ref(false)
const highresReady = ref(false)
const highresFailed = ref(false)
const placeholderFailed = ref(false)
const lightboxOpen = ref(false)
const lightboxFallbackUsed = ref(false)
const lightboxClose = ref(null)
const lightboxRef = ref(null)
const coverStage = ref(null)
useModalFocus(lightboxRef, lightboxOpen, {
  initialFocusRef: lightboxClose,
  restoreFocusRef: coverStage,
})
const items = computed(() => (props.videos || []).slice(0, 4))
const active = computed(() => items.value[index.value] || null)
const activeKey = computed(() => active.value?.viewkey || active.value?.url || index.value)
const localPreviewUrl = computed(() => resolveApiAssetUrl(active.value?.preview_image_url))
const displayUrl = computed(() => (
  localPreviewUrl.value && !highresFailed.value
    ? localPreviewUrl.value
    : (active.value?.thumbnail || '')
))
let pointer = null
let suppressClickUntil = 0

watch(() => props.identity, () => {
  index.value = 0
  closeLightbox()
})
watch(() => items.value.length, length => {
  if (!length || index.value >= length) index.value = 0
})
watch([activeKey, localPreviewUrl, () => active.value?.thumbnail], () => {
  highresReady.value = false
  highresFailed.value = false
  placeholderFailed.value = false
  lightboxFallbackUsed.value = false
})
watch(lightboxOpen, open => {
  if (open) {
    window.addEventListener('keydown', onWindowKeydown)
  } else {
    window.removeEventListener('keydown', onWindowKeydown)
  }
})
onBeforeUnmount(() => window.removeEventListener('keydown', onWindowKeydown))
onDeactivated(closeLightbox)

function show(nextIndex) {
  const length = items.value.length
  if (length > 1) index.value = (nextIndex + length) % length
}
function previous() { show(index.value - 1) }
function next() { show(index.value + 1) }

function pointerDown(event) {
  if (items.value.length < 2 || (event.pointerType === 'mouse' && event.button !== 0)) return
  pointer = { id: event.pointerId, x: event.clientX, y: event.clientY }
  dragging.value = true
}
function pointerMove(event) {
  if (!pointer || pointer.id !== event.pointerId) return
  const dx = event.clientX - pointer.x
  const dy = event.clientY - pointer.y
  if (Math.abs(dx) > 8 && Math.abs(dx) > Math.abs(dy)) {
    // 轻点保留内部按钮的 click 目标；确定横向拖动后才由轮播接管指针。
    try { event.currentTarget.setPointerCapture?.(event.pointerId) } catch (_) {}
    event.preventDefault()
  }
}
function pointerEnd(event, cancelled = false) {
  if (!pointer || pointer.id !== event.pointerId) return
  const dx = event.clientX - pointer.x
  const dy = event.clientY - pointer.y
  const threshold = Math.max(36, Math.min(64, event.currentTarget.clientWidth * 0.14))
  if (Math.abs(dx) > 8 && Math.abs(dx) > Math.abs(dy)) suppressClickUntil = Date.now() + 400
  if (!cancelled && Math.abs(dx) >= threshold && Math.abs(dx) > Math.abs(dy) * 1.15) {
    dx < 0 ? next() : previous()
  }
  try { event.currentTarget.releasePointerCapture?.(event.pointerId) } catch (_) {}
  pointer = null
  dragging.value = false
}
function openLightbox(event) {
  if (Date.now() < suppressClickUntil) {
    event?.preventDefault()
    event?.stopPropagation()
    return
  }
  lightboxFallbackUsed.value = false
  lightboxOpen.value = true
}
function closeLightbox() {
  lightboxOpen.value = false
}
function onWindowKeydown(event) {
  if (!lightboxOpen.value || !isTopModal(lightboxRef.value)) return
  if (event.key === 'Escape') closeLightbox()
  else if (event.key === 'ArrowLeft') previous()
  else if (event.key === 'ArrowRight') next()
}
function onLightboxImageError(event) {
  const fallback = active.value?.thumbnail
  if (!lightboxFallbackUsed.value && fallback && event.currentTarget.src !== fallback) {
    lightboxFallbackUsed.value = true
    event.currentTarget.src = fallback
    return
  }
  event.currentTarget.classList.add('is-broken')
}
function onStageImageError(event) {
  // 淡出中的上一张图片也可能迟到触发 error；不能让它污染新封面的回退状态。
  const failedUrl = event?.currentTarget?.getAttribute('src') || ''
  if (failedUrl && failedUrl !== displayUrl.value) return
  if (localPreviewUrl.value && !highresFailed.value) {
    highresFailed.value = true
    highresReady.value = false
    if (!active.value?.thumbnail) placeholderFailed.value = true
    return
  }
  placeholderFailed.value = true
  highresReady.value = false
}
function duration(seconds) {
  const value = Number(seconds || 0)
  if (!value) return ''
  const hours = Math.floor(value / 3600)
  const minutes = Math.floor((value % 3600) / 60)
  const secs = value % 60
  return hours
    ? `${hours}:${String(minutes).padStart(2, '0')}:${String(secs).padStart(2, '0')}`
    : `${minutes}:${String(secs).padStart(2, '0')}`
}
</script>

<template>
  <div v-if="items.length" class="discovery-covers shared-covers">
    <div ref="coverStage" class="discovery-cover-stage" :class="{
           'is-draggable': items.length > 1, 'is-dragging': dragging,
         }" role="region" aria-roledescription="轮播图" tabindex="0"
         :aria-label="`${name} 的视频封面`"
         @pointerdown="pointerDown" @pointermove="pointerMove"
         @pointerup="pointerEnd" @pointercancel="event => pointerEnd(event, true)"
         @keydown.left.prevent="previous" @keydown.right.prevent="next">
      <Transition name="discovery-cover-fade">
        <button v-if="active" :key="`${activeKey}-${index}`"
                class="discovery-cover" type="button"
                :aria-label="`查看大图：${active.title || `${name} 的作品封面`}`"
                :title="active.title || `查看 ${name} 的作品封面`"
                @click="openLightbox" @dragstart.prevent>
          <img v-if="displayUrl && !placeholderFailed"
               class="discovery-cover-placeholder is-ready"
               :src="displayUrl" :alt="active.title || `${name} 的作品封面`"
               loading="lazy" decoding="async" referrerpolicy="origin"
               draggable="false" width="640" height="360"
               @load="highresReady = true" @error="onStageImageError" />
          <span v-if="!displayUrl || placeholderFailed || !highresReady"
               :class="['discovery-cover-empty', {
                  'discovery-cover-fallback': displayUrl,
                }]" role="status">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"
                 aria-hidden="true"><rect x="3" y="5" width="18" height="14" rx="2"/><path d="m3 16 5-5 4 4 3-3 6 6"/></svg>
            <small>{{ placeholderFailed ? '封面暂时不可用' : '正在准备清晰封面' }}</small>
          </span>
          <span class="discovery-cover-zoom" aria-hidden="true">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <circle cx="11" cy="11" r="6"/><path d="m16 16 4 4M11 8v6M8 11h6"/>
            </svg>
            查看大图
          </span>
          <span v-if="duration(active.duration)" class="discovery-duration">
            {{ duration(active.duration) }}
          </span>
        </button>
      </Transition>
      <a v-if="active?.url || fallbackUrl" class="discovery-video-link"
         :href="active?.url || fallbackUrl" target="_blank" rel="noopener noreferrer"
         :aria-label="`打开作品：${active?.title || name}`"
         @pointerdown.stop @click.stop>打开作品</a>
      <template v-if="items.length > 1">
        <button class="discovery-cover-nav previous" type="button"
                :aria-label="`${name}：上一张封面`" @pointerdown.stop @click.stop="previous">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><path d="m15 18-6-6 6-6"/></svg>
        </button>
        <button class="discovery-cover-nav next" type="button"
                :aria-label="`${name}：下一张封面`" @pointerdown.stop @click.stop="next">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><path d="m9 18 6-6-6-6"/></svg>
        </button>
        <span class="discovery-cover-count" aria-hidden="true">{{ index + 1 }} / {{ items.length }}</span>
      </template>
    </div>
    <span class="sr-only" aria-live="polite">当前第 {{ index + 1 }} 张，共 {{ items.length }} 张</span>
  </div>
  <div v-else class="discovery-covers-empty shared-covers-empty">暂无可用视频封面</div>

  <Teleport to="body">
    <Transition name="cover-lightbox-fade">
      <div v-if="lightboxOpen && active" ref="lightboxRef" class="cover-lightbox"
           role="dialog" aria-modal="true" tabindex="-1"
           :aria-label="`${name} 的视频封面大图`" @click.self="closeLightbox">
        <div class="cover-lightbox-panel">
          <div class="cover-lightbox-header">
            <div class="cover-lightbox-heading">
              <strong>{{ active.title || `${name} 的作品封面` }}</strong>
              <span>{{ index + 1 }} / {{ items.length }} · 清晰封面</span>
            </div>
            <button ref="lightboxClose" class="cover-lightbox-close" type="button"
                    aria-label="关闭大图" @click="closeLightbox">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M6 6l12 12M18 6 6 18"/></svg>
            </button>
          </div>
          <div class="cover-lightbox-image-wrap">
            <img :key="`lightbox-${activeKey}`"
                 :src="localPreviewUrl || active.thumbnail"
                 :alt="active.title || `${name} 的作品封面大图`"
                 decoding="async" referrerpolicy="origin" width="640" height="360"
                 @error="onLightboxImageError" />
            <template v-if="items.length > 1">
              <button class="cover-lightbox-nav previous" type="button"
                      :aria-label="`${name}：上一张大图`" @click="previous">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><path d="m15 18-6-6 6-6"/></svg>
              </button>
              <button class="cover-lightbox-nav next" type="button"
                      :aria-label="`${name}：下一张大图`" @click="next">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><path d="m9 18 6-6-6-6"/></svg>
              </button>
            </template>
          </div>
          <div class="cover-lightbox-footer">
            <span>可用方向键切换，Esc 关闭</span>
            <a v-if="active.url || fallbackUrl" :href="active.url || fallbackUrl"
               target="_blank" rel="noopener noreferrer">打开这部作品</a>
          </div>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>
