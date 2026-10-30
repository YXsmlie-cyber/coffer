<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import api from '../api'
import { apiErrorText } from '../utils/apiError'

const emit = defineEmits(['feedback'])

// 事件顺序需与后端 notifier._EVENT_KEYS 一致。
const EVENT_LABELS = [
  ['patrol_summary', '巡查汇总'],
  ['new_missing', '发现新缺失'],
  ['batch_done', '批次完成'],
  ['download_done', '逐条下载完成（自动合并）'],
  ['upload_done', '逐条上传完成（自动合并）'],
  ['error', '错误与异常'],
  ['daily_digest', '每日摘要（每天 9:00）'],
]

const enabled = ref(false)
const barkUrl = ref('')
const urlHint = ref('')
const configured = ref(false)
const events = reactive({
  patrol_summary: true,
  new_missing: true,
  batch_done: true,
  download_done: false,
  upload_done: false,
  error: true,
  daily_digest: true,
})
const busy = ref(false)
const expanded = ref(false)
const savedSettings = ref(null)
const dirty = computed(() => savedSettings.value !== null && (
  enabled.value !== savedSettings.value.enabled
  || Boolean(barkUrl.value.trim())
  || EVENT_LABELS.some(([key]) => events[key] !== savedSettings.value.events[key])
))

function rememberSavedSettings() {
  savedSettings.value = { enabled: enabled.value, events: { ...events } }
}

function feedback(text, type = '') {
  emit('feedback', { text, type })
}

async function loadStatus() {
  try {
    const { data } = await api.notifyStatus()
    enabled.value = !!data.enabled
    configured.value = !!data.configured
    urlHint.value = data.url_hint || ''
    for (const [key] of EVENT_LABELS) {
      if (data.events && key in data.events) events[key] = !!data.events[key]
    }
    rememberSavedSettings()
  } catch (_) { /* 后端未就绪时保持默认 */ }
}

async function save() {
  if (busy.value) return
  busy.value = true
  try {
    const { data } = await api.notifyConfig({
      enabled: enabled.value,
      // 仅在用户输入了新地址时提交；留空则保留后端已存的地址。
      bark_url: barkUrl.value.trim() || undefined,
      events: { ...events },
    })
    enabled.value = !!data.enabled
    configured.value = !!data.configured
    urlHint.value = data.url_hint || ''
    barkUrl.value = ''
    rememberSavedSettings()
    feedback('通知设置已保存')
  } catch (error) {
    feedback(`保存通知设置失败：${apiErrorText(error)}`, 'err')
  } finally {
    busy.value = false
  }
}

async function sendTest() {
  if (busy.value) return
  busy.value = true
  try {
    await api.notifyTest()
    feedback('测试通知已发送，请查看手机')
  } catch (error) {
    feedback(`测试通知失败：${apiErrorText(error)}`, 'err')
  } finally {
    busy.value = false
  }
}

onMounted(loadStatus)
</script>

<template>
  <section class="notify-bar" :class="{ on: enabled }" aria-label="Bark 通知" :aria-busy="busy">
    <button class="btn" :class="enabled ? '' : 'secondary'"
            :disabled="busy" @click="enabled = !enabled">
      Bark 通知：{{ enabled ? '开' : '关' }}
    </button>
    <button class="btn secondary" :disabled="busy"
            :aria-expanded="expanded" @click="expanded = !expanded">
      {{ expanded ? '收起设置' : '通知设置' }}
    </button>
    <span v-if="dirty" class="notify-unsaved" role="status">未保存 · 请在通知设置中保存</span>
    <span class="spacer"></span>
    <span v-if="configured" class="hint" role="status">
      已配置地址 {{ urlHint }}
    </span>
    <span v-else class="hint">尚未配置推送地址</span>

    <div v-if="expanded" class="notify-panel">
      <label class="notify-label" for="bark-url">Bark 地址</label>
      <input id="bark-url" v-model="barkUrl" type="url"
             :placeholder="configured ? `已保存（${urlHint}），留空则不修改` : 'https://api.day.app/你的Key'"
             style="flex:1;min-width:240px"
             aria-label="Bark 推送地址" />
      <fieldset class="notify-events">
        <legend class="sr-only">推送事件</legend>
        <label v-for="[key, label] in EVENT_LABELS" :key="key" class="notify-check">
          <input type="checkbox" v-model="events[key]" />{{ label }}
        </label>
      </fieldset>
      <button class="btn" :class="{ 'notify-save-dirty': dirty }" :disabled="busy" @click="save">保存</button>
      <button class="btn secondary" :disabled="busy || !configured" @click="sendTest">
        发送测试
      </button>
    </div>
  </section>
</template>
