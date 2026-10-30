<script setup>
import { computed, ref } from 'vue'
import { dialogState, _settle } from '../composables/useDialog'
import { useEscClose } from '../composables/useEscClose'
import { useModalFocus } from '../composables/useModalFocus'

// Esc = 取消（alert 模式下等同关闭）
const modalRef = ref(null)
const isOpen = computed(() => dialogState.open)
useEscClose(() => { if (dialogState.open) _settle(false) }, modalRef)
useModalFocus(modalRef, isOpen)
</script>

<template>
  <Transition name="fade">
    <div v-if="dialogState.open" class="modal-mask" @click.self="_settle(false)">
      <div ref="modalRef" class="modal modal-sm" role="alertdialog" aria-modal="true"
           aria-labelledby="cd-title" aria-describedby="cd-msg" tabindex="-1">
        <h3 id="cd-title">{{ dialogState.title }}</h3>
        <p id="cd-msg" class="cd-message">{{ dialogState.message }}</p>
        <div class="cd-actions">
          <button v-if="!dialogState.alertOnly" class="btn secondary" @click="_settle(false)">
            {{ dialogState.cancelText }}
          </button>
          <button class="btn" :class="{ 'danger-solid': dialogState.danger }" @click="_settle(true)">
            {{ dialogState.confirmText }}
          </button>
        </div>
      </div>
    </div>
  </Transition>
</template>
