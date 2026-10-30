<script setup>
import { computed, ref, watch } from 'vue'
import { resolveApiAssetUrl } from '../utils/assetUrl'
import CoverCarousel from './CoverCarousel.vue'

const props = defineProps({
  item: { type: Object, required: true },
  busy: { type: Boolean, default: false },
})
const emit = defineEmits(['feedback'])

const avatarFailed = ref(false)
const model = computed(() => props.item.model || {})
const avatarUrl = computed(() => resolveApiAssetUrl(model.value.avatar_url))
const titleId = computed(() => `taste-result-${model.value.id}`)
// 推荐说明：与你收藏（或旧模式下喜欢）的哪一位最像，没有可比对象时用通用说明。
const similar = computed(() => props.item.similar_to || null)
const similarAvatarFailed = ref(false)
const similarAvatar = computed(() => (
  similar.value && !similarAvatarFailed.value ? resolveApiAssetUrl(similar.value.avatar_url) : ''))

watch(() => model.value.id, () => {
  avatarFailed.value = false
  similarAvatarFailed.value = false
})
</script>

<template>
  <article class="taste-result-card" :aria-labelledby="titleId">
    <header class="taste-result-card-head">
      <span class="taste-result-rank">#{{ item.rank }}</span>
      <span class="taste-result-level">{{ item.match_level }}</span>
    </header>

    <div class="taste-result-identity">
      <div class="taste-result-avatar-wrap">
        <img v-if="avatarUrl && !avatarFailed" class="taste-result-avatar"
             :src="avatarUrl" :alt="`${model.name} 的头像`"
             width="64" height="64" loading="lazy" decoding="async"
             @error="avatarFailed = true" />
        <span v-else class="taste-avatar-fallback" aria-hidden="true">
          {{ model.name?.trim()?.slice(0, 1)?.toUpperCase() || '?' }}
        </span>
      </div>
      <div class="taste-result-name">
        <h3 :id="titleId">{{ model.name }}</h3>
        <p v-if="model.profile_slug">@{{ model.profile_slug }}</p>
      </div>
      <a class="taste-profile-link" :href="model.url" target="_blank"
         rel="noopener noreferrer" :aria-label="`在 PornHub 打开 ${model.name} 的主页`">
        主页
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
             stroke-width="2" aria-hidden="true">
          <path d="M14 5h5v5M10 14 19 5M19 13v5a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1h5" />
        </svg>
      </a>
    </div>

    <CoverCarousel :videos="model.preview_videos" :name="model.name"
                   :fallback-url="model.url" :identity="`result-${model.id}`" />
    <p class="taste-result-reason" :title="item.reason">
      <template v-if="similar">
        <img v-if="similarAvatar" class="taste-similar-avatar" :src="similarAvatar" alt=""
             width="22" height="22" loading="lazy" decoding="async"
             @error="similarAvatarFailed = true" />
        <span>像你{{ item.similar_label || '喜欢' }}的 <strong>{{ similar.name }}</strong></span>
      </template>
      <span v-else>{{ item.reason }}</span>
    </p>

    <div class="taste-result-feedback" role="group"
         :aria-label="`评价 ${model.name} 是否符合你的审美`">
      <button type="button" class="taste-feedback-button like"
              :class="{ selected: item.feedback === 'like' }"
              :aria-pressed="item.feedback === 'like'" :disabled="busy"
              @click="emit('feedback', model.id, 'like')">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
             stroke-width="2" aria-hidden="true"><path d="m5 12 4 4L19 6" /></svg>
        {{ item.feedback === 'like' ? '已标为符合' : '符合' }}
      </button>
      <button type="button" class="taste-feedback-button dislike"
              :class="{ selected: item.feedback === 'dislike' }"
              :aria-pressed="item.feedback === 'dislike'" :disabled="busy"
              @click="emit('feedback', model.id, 'dislike')">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
             stroke-width="2" aria-hidden="true"><path d="m7 7 10 10M17 7 7 17" /></svg>
        {{ item.feedback === 'dislike' ? '已标为不符合' : '不符合' }}
      </button>
    </div>
  </article>
</template>
