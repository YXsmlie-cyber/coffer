<script setup>
import { computed, ref, watch } from 'vue'
import CoverCarousel from './CoverCarousel.vue'
import VideoList from './VideoList.vue'
import { useEscClose } from '../composables/useEscClose'
import { useModalFocus } from '../composables/useModalFocus'
import { resolveApiAssetUrl } from '../utils/assetUrl'
import { formatDateTime } from '../utils/formatters'
import {
  avatarStatusText, downloadableCount, modelProgress, modelState, rankTone,
} from '../utils/modelMedia'

// 右侧详情抽屉：封面、统计、操作与视频勾选合在一处，网格保留在遮罩后方。
const props = defineProps({
  model: { type: Object, required: true },
  syncing: { type: Boolean, default: false },
  index: { type: Number, default: 0 },
  total: { type: Number, default: 1 },
  message: { type: String, default: '' },
  messageType: { type: String, default: '' },
})
const emit = defineEmits(['close', 'step', 'download', 'sync', 'remove', 'queued'])

const panelRef = ref(null)
const scrollRef = ref(null)
useEscClose(() => emit('close'), panelRef)
useModalFocus(panelRef)

const avatarFailed = ref(false)
watch(() => props.model.id, () => {
  avatarFailed.value = false
  if (scrollRef.value) scrollRef.value.scrollTop = 0
})

const avatarUrl = computed(() => (
  avatarFailed.value ? '' : resolveApiAssetUrl(props.model.avatar_url)))
const state = computed(() => modelState(props.model, props.syncing))
const progress = computed(() => modelProgress(props.model))
const downloadable = computed(() => downloadableCount(props.model))
const pendingUpload = computed(() => Number(props.model.pending_upload_videos || 0))

const metaText = computed(() => {
  const parts = []
  if (props.model.ph_video_count) parts.push(`${props.model.ph_video_count} 视频`)
  if (props.model.views_text) parts.push(`${props.model.views_text} 观看`)
  parts.push(props.model.synced_at
    ? `同步于 ${formatDateTime(props.model.synced_at, '未知时间')}`
    : '尚未同步')
  return parts.join(' · ')
})

const downloadText = computed(() => {
  if (downloadable.value > 0) return `下载缺失 ${downloadable.value}`
  if (pendingUpload.value > 0) return `待上传 ${pendingUpload.value}`
  return '无可下载'
})
</script>

<template>
  <div class="drawer-scrim" @click.self="emit('close')">
    <aside ref="panelRef" class="model-drawer" role="dialog" aria-modal="true"
           aria-labelledby="model-drawer-title" tabindex="-1">
      <header class="drawer-bar">
        <div class="drawer-steps">
          <button type="button" class="drawer-icon" :disabled="index <= 0"
                  aria-label="上一位模特" @click="emit('step', -1)">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"
                 aria-hidden="true"><path d="m15 18-6-6 6-6" /></svg>
          </button>
          <span class="drawer-position">{{ index + 1 }} / {{ total }}</span>
          <button type="button" class="drawer-icon" :disabled="index >= total - 1"
                  aria-label="下一位模特" @click="emit('step', 1)">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"
                 aria-hidden="true"><path d="m9 18 6-6-6-6" /></svg>
          </button>
        </div>
        <button type="button" class="drawer-icon" aria-label="关闭详情" title="关闭（Esc）"
                @click="emit('close')">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
               aria-hidden="true"><path d="M6 6l12 12M18 6 6 18" /></svg>
        </button>
      </header>

      <div ref="scrollRef" class="drawer-scroll">
        <CoverCarousel :videos="model.preview_videos || []" :name="model.name"
                       :fallback-url="model.url" :identity="model.id" />

        <section class="drawer-identity">
          <a :href="model.url" target="_blank" rel="noopener noreferrer"
             class="drawer-avatar" :title="`在 PornHub 打开 ${model.name} 的主页`">
            <img v-if="avatarUrl" :src="avatarUrl" :alt="`${model.name} 的头像`"
                 decoding="async" width="72" height="72" @error="avatarFailed = true" />
            <span v-else class="drawer-avatar-state" :data-status="model.avatar_status || 'pending'">
              {{ avatarStatusText(model.avatar_status) }}
            </span>
          </a>
          <div class="drawer-identity-text">
            <div class="drawer-tags">
              <span v-if="model.rank" class="media-rank" :class="rankTone(model.rank)">#{{ model.rank }}</span>
              <span class="media-state" :class="state.tone">{{ state.text }}</span>
            </div>
            <h3 id="model-drawer-title">{{ model.name }}</h3>
            <p class="drawer-meta">{{ metaText }}</p>
          </div>
        </section>

        <dl class="drawer-stats">
          <div class="v-missing"><dt>缺失</dt><dd>{{ model.missing_videos || 0 }}</dd></div>
          <div class="v-maybe"><dt>疑似</dt><dd>{{ model.maybe_videos || 0 }}</dd></div>
          <div class="v-exists"><dt>已有</dt><dd>{{ model.cloud_videos || 0 }}</dd></div>
          <div><dt>共计</dt><dd>{{ model.total_videos || 0 }}</dd></div>
          <div class="v-pending"><dt>待上传</dt><dd>{{ pendingUpload }}</dd></div>
        </dl>
        <div class="drawer-meter" role="progressbar" aria-label="115 同步率"
             :aria-valuenow="progress" aria-valuemin="0" aria-valuemax="100">
          <span :style="{ '--progress': progress / 100 }"></span>
          <small>115 同步率 {{ progress }}%</small>
        </div>

        <div class="drawer-actions">
          <button type="button" class="btn" :disabled="!model.synced_at || downloadable === 0"
                  @click="emit('download', model, 'missing')">{{ downloadText }}</button>
          <button type="button" class="btn secondary" :disabled="syncing"
                  @click="emit('sync', model.id)">{{ syncing ? '同步中…' : '同步' }}</button>
          <a class="btn secondary" :href="model.url" target="_blank"
             rel="noopener noreferrer">打开主页</a>
          <button type="button" class="btn danger" @click="emit('remove', model)">删除模特</button>
        </div>

        <div class="drawer-feedback" aria-live="polite" aria-atomic="true">
          <p v-if="message" class="toast" :class="messageType"
             :role="messageType === 'err' ? 'alert' : 'status'">{{ message }}</p>
        </div>

        <section class="drawer-videos" aria-labelledby="drawer-videos-title">
          <h4 id="drawer-videos-title">视频</h4>
          <VideoList v-if="model.synced_at" :key="model.id" :model="model"
                     @queued="added => emit('queued', added)" />
          <div v-else class="drawer-unsynced">
            <p>尚未与 115 对比。同步后可查看缺失视频并手动勾选下载（含疑似）。</p>
            <button type="button" class="btn" :disabled="syncing"
                    @click="emit('sync', model.id)">{{ syncing ? '同步中…' : '立即同步' }}</button>
          </div>
        </section>
      </div>
    </aside>
  </div>
</template>
