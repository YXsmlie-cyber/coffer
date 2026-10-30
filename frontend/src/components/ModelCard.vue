<script setup>
import { computed, ref, watch } from 'vue'
import { resolveApiAssetUrl } from '../utils/assetUrl'
import {
  coverSources, downloadableCount, modelProgress, modelState, rankTone,
} from '../utils/modelMedia'

// 封面在上、文字在下的媒体卡：整卡打开详情抽屉；快捷下载只在可悬停设备上浮现。
const props = defineProps({
  model: { type: Object, required: true },
  syncing: { type: Boolean, default: false },
  active: { type: Boolean, default: false },
})
const emit = defineEmits(['open', 'download'])

const sources = computed(() => coverSources(props.model))
const sourceIndex = ref(0)
const avatarFailed = ref(false)
watch(sources, () => { sourceIndex.value = 0 })
watch(() => props.model.avatar_url, () => { avatarFailed.value = false })

const coverUrl = computed(() => sources.value[sourceIndex.value] || '')
// 封面加载完成后淡入；换图时重新等待。缓存命中的图片挂载时已 complete，直接显示。
const coverLoaded = ref(false)
watch(coverUrl, () => { coverLoaded.value = false })
function trackCover(element) {
  if (element?.complete && element.naturalWidth > 0) coverLoaded.value = true
}
const avatarUrl = computed(() => (
  avatarFailed.value ? '' : resolveApiAssetUrl(props.model.avatar_url)))
const state = computed(() => modelState(props.model, props.syncing))
const progress = computed(() => modelProgress(props.model))
const downloadable = computed(() => downloadableCount(props.model))
const pendingUpload = computed(() => Number(props.model.pending_upload_videos || 0))
const initial = computed(() => String(props.model.name || '?').trim().slice(0, 1).toUpperCase())

const metaText = computed(() => {
  const model = props.model
  if (model.synced_at) return `115 已有 ${model.cloud_videos || 0} / ${model.total_videos || 0}`
  const parts = []
  if (model.ph_video_count) parts.push(`${model.ph_video_count} 视频`)
  if (model.views_text) parts.push(`${model.views_text} 观看`)
  return parts.join(' · ') || '尚未同步'
})

const label = computed(() => [
  props.model.rank ? `第 ${props.model.rank} 名` : '',
  props.model.name,
  state.value.text,
  metaText.value,
  '打开详情',
].filter(Boolean).join('，'))
</script>

<template>
  <article class="media-card" :class="{ 'is-active': active }" :data-model-id="model.id">
    <button type="button" class="media-card-hit" aria-haspopup="dialog"
            :aria-label="label" @click="emit('open', model)">
      <span class="media-card-cover">
        <img v-if="coverUrl" :key="coverUrl" :ref="trackCover" :src="coverUrl" alt=""
             :class="{ 'is-pending': !coverLoaded }"
             loading="lazy" decoding="async" referrerpolicy="origin"
             draggable="false" width="640" height="360"
             @load="coverLoaded = true" @error="sourceIndex += 1" />
        <span v-else class="media-card-cover-empty">
          <img v-if="avatarUrl" :src="avatarUrl" alt="" loading="lazy" decoding="async"
               width="104" height="104" @error="avatarFailed = true" />
          <small>暂无作品封面</small>
        </span>
        <span v-if="model.synced_at" class="media-card-meter" aria-hidden="true">
          <span :style="{ '--progress': progress / 100 }"></span>
        </span>
      </span>

      <span class="media-card-badges">
        <span v-if="model.rank" class="media-rank" :class="rankTone(model.rank)">#{{ model.rank }}</span>
        <span class="media-state" :class="state.tone">{{ state.text }}</span>
        <span v-if="state.tone === 'missing' && pendingUpload" class="media-state pending">
          待上传 {{ pendingUpload }}
        </span>
      </span>

      <span class="media-card-info">
        <span class="media-avatar">
          <img v-if="avatarUrl" :src="avatarUrl" alt="" loading="lazy" decoding="async"
               width="36" height="36" @error="avatarFailed = true" />
          <span v-else aria-hidden="true">{{ initial }}</span>
        </span>
        <span class="media-card-text">
          <strong :title="model.name">{{ model.name }}</strong>
          <small>{{ metaText }}</small>
        </span>
      </span>
    </button>

    <button v-if="model.synced_at && downloadable > 0" type="button"
            class="btn sm media-card-quick" :aria-label="`下载 ${model.name} 的 ${downloadable} 个缺失视频`"
            @click="emit('download', model, 'missing')">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
           stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <path d="M12 4v11m0 0-4-4m4 4 4-4M5 19h14" />
      </svg>
      下载 {{ downloadable }}
    </button>
  </article>
</template>
