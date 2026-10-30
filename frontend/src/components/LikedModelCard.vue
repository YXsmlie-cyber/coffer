<script setup>
import { computed } from 'vue'
import { resolveApiAssetUrl } from '../utils/assetUrl'
import { formatLikedDate } from '../utils/formatters'
import CoverCarousel from './CoverCarousel.vue'

const props = defineProps({
  model: { type: Object, required: true },
  selected: { type: Boolean, default: false },
  syncing: { type: Boolean, default: false },
  busy: { type: Boolean, default: false },
})

const emit = defineEmits(['toggle', 'sync', 'download', 'detail'])

const avatarUrl = computed(() => resolveApiAssetUrl(props.model.avatar_url))
const downloadable = computed(() => Number(
  props.model.downloadable_videos ?? props.model.missing_videos ?? 0))
const synced = computed(() => Boolean(props.model.synced_at))
const progress = computed(() => {
  const total = Number(props.model.total_videos || 0)
  return total > 0
    ? Math.min(100, Math.round(Number(props.model.cloud_videos || 0) / total * 100))
    : 0
})

function avatarStatusText(status) {
  return {
    source_no_avatar: '源站无头像',
    source_unavailable: '主页已失效',
    source_broken: '源头像失效',
    fetch_error: '头像读取失败',
    ready: '头像文件异常',
    pending: '头像未入库',
  }[status] || '头像未入库'
}

function onToggle(event) {
  emit('toggle', props.model.id, Boolean(event.target.checked))
}
</script>

<template>
  <article class="liked-model-card" :class="{ selected }" :data-model-id="model.id">
    <div class="liked-card-topline">
      <label class="liked-card-select">
        <input type="checkbox" :checked="selected"
               :aria-label="`选择 ${model.name}`" @change="onToggle" />
        <span>选择</span>
      </label>
      <div class="liked-card-badges">
        <span class="liked-state-badge liked">符合</span>
        <span class="liked-state-badge" :class="{ ready: synced }">
          {{ synced ? '已校准库存' : '待校准库存' }}
        </span>
      </div>
    </div>

    <div class="liked-card-identity">
      <a :href="model.url" target="_blank" rel="noopener noreferrer"
         class="liked-avatar-link" :title="`打开 ${model.name} 的主页`">
        <img v-if="avatarUrl" :src="avatarUrl" :alt="`${model.name} 的头像`"
             width="72" height="72" loading="lazy" decoding="async" />
        <span v-else role="img"
              :aria-label="`${model.name}：${avatarStatusText(model.avatar_status)}`">
          {{ String(model.name || '?').slice(0, 1).toUpperCase() }}
        </span>
      </a>
      <div class="liked-card-name">
        <h4 :title="model.name">{{ model.name }}</h4>
        <p>{{ formatLikedDate(model.liked_at) }}加入 · 本机偏好记录</p>
        <a :href="model.url" target="_blank" rel="noopener noreferrer">查看主页</a>
      </div>
    </div>

    <CoverCarousel :videos="model.preview_videos || []" :name="model.name"
                   :fallback-url="model.url" :identity="`liked-${model.id}`" />

    <div class="liked-card-stats" aria-label="视频库存统计">
      <div><strong>{{ model.missing_videos || 0 }}</strong><span>明确缺失</span></div>
      <div><strong>{{ model.maybe_videos || 0 }}</strong><span>疑似已有</span></div>
      <div><strong>{{ model.cloud_videos || 0 }}</strong><span>115 已有</span></div>
      <div><strong>{{ model.total_videos || 0 }}</strong><span>作品总数</span></div>
    </div>

    <div class="liked-card-meter" :title="`115 已有 ${progress}%`">
      <span :style="{ '--progress': progress / 100 }"></span>
    </div>

    <div class="liked-card-state" :class="{
           warning: !synced,
           active: synced && (downloadable > 0 || model.pending_upload_videos),
         }">
      <template v-if="!synced">
        <strong>先校准库存</strong>
        <span>同步后才会计算明确缺失，不会自动下载</span>
      </template>
      <template v-else-if="model.pending_upload_videos">
        <strong>{{ model.pending_upload_videos }} 个待上传</strong>
        <span>下载完成后由现有上传队列处理</span>
      </template>
      <template v-else-if="downloadable > 0">
        <strong>{{ downloadable }} 个可加入下载</strong>
        <span>不包含疑似已有与已在流水线中的视频</span>
      </template>
      <template v-else>
        <strong>当前无需下载</strong>
        <span>没有新的明确缺失视频</span>
      </template>
    </div>

    <div class="liked-card-actions">
      <button type="button" class="btn amber"
              :disabled="busy || syncing || !synced || downloadable === 0"
              :title="!synced ? '请先校准库存' : '确认后加入明确缺失视频'"
              @click="emit('download', model)">
        {{ busy ? '处理中…' : `下载缺失 ${downloadable || ''}`.trim() }}
      </button>
      <button type="button" class="btn secondary"
              :disabled="busy || syncing || !synced"
              title="查看视频并手动选择；提交前仍会再次确认"
              @click="emit('detail', model)">逐个选择</button>
      <button type="button" class="btn secondary"
              :disabled="busy || syncing" @click="emit('sync', model.id)">
        {{ syncing ? '同步中…' : synced ? '重新校准' : '校准库存' }}
      </button>
    </div>
  </article>
</template>
