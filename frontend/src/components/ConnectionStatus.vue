<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { subscribeWSState } from '../api/websocket'

const state = ref({ status: 'idle', connectedAt: null })
const text = computed(() => ({
  idle: '实时连接中', connecting: '实时连接中', connected: '实时已连接',
  reconnecting: '实时重连中', offline: '网络已断开', auth_required: '请重新登录',
})[state.value.status])
const title = computed(() => state.value.status === 'connected'
  ? '连接恢复或返回页面时，会自动刷新当前数据'
  : '连接恢复后会自动校准任务状态；当前数据可能尚未更新')
let unsubscribe = null
onMounted(() => { unsubscribe = subscribeWSState(value => { state.value = value }) })
onUnmounted(() => unsubscribe?.())
</script>

<template>
  <span class="connection-status" :class="state.status" :title="title"
        role="status" aria-live="polite">{{ text }}</span>
</template>
