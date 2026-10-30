<script setup>
import { ref } from 'vue'
import VideoList from './VideoList.vue'
import { useEscClose } from '../composables/useEscClose'
import { useModalFocus } from '../composables/useModalFocus'

defineProps({
  model: { type: Object, required: true },
  confirmBeforeQueue: { type: Boolean, default: false },
})
const emit = defineEmits(['close', 'queued'])

const modalRef = ref(null)
useEscClose(() => emit('close'), modalRef)
useModalFocus(modalRef)

// 弹窗保留原行为：加入队列后通知父页面并关闭。
function onQueued(added) {
  emit('queued', added)
  emit('close')
}
</script>

<template>
  <div class="modal-mask" @click.self="emit('close')">
    <div ref="modalRef" class="modal modal-wide" role="dialog" aria-modal="true"
         aria-labelledby="vlm-title" tabindex="-1">
      <h3 id="vlm-title">{{ model.name }} · 视频列表</h3>
      <VideoList :model="model" :confirm-before-queue="confirmBeforeQueue" closable
                 @close="emit('close')" @queued="onQueued" />
    </div>
  </div>
</template>
