<script setup>
import { computed, reactive, ref, watch } from 'vue'
import api from '../api'
import { apiErrorText } from '../utils/apiError'

// 下载运行参数：并发、连接预算、近似总限速、下载时段与防睡眠。显式保存后立即
// 生效并写入 runtime_settings；已在跑的任务不受并发下调影响，新尝试按新值执行。
const props = defineProps({
  settings: { type: Object, default: null },
})
const emit = defineEmits(['saved', 'close'])

const form = reactive({
  max_concurrent_downloads: 5,
  max_connections: 15,
  rate_limit_mbps: 0,
  window_start: '',
  window_end: '',
  keep_awake: true,
})
const saving = ref(false)
const error = ref('')

function fill(values) {
  if (!values) return
  for (const key of Object.keys(form)) {
    if (key in values) form[key] = values[key] ?? form[key]
  }
  error.value = ''
}
watch(() => props.settings, fill, { immediate: true })

const effective = computed(() => Math.max(1, Math.min(
  Number(form.max_concurrent_downloads) || 1,
  Math.floor((Number(form.max_connections) || 2) / 2),
)))
const dirty = computed(() => Boolean(props.settings) && Object.keys(form).some(
  key => String(form[key] ?? '') !== String(props.settings[key] ?? '')))

async function save() {
  if (saving.value) return
  saving.value = true
  error.value = ''
  try {
    const { data } = await api.saveDownloadSettings({
      max_concurrent_downloads: Number(form.max_concurrent_downloads),
      max_connections: Number(form.max_connections),
      rate_limit_mbps: Number(form.rate_limit_mbps) || 0,
      window_start: form.window_start || '',
      window_end: form.window_end || '',
      keep_awake: Boolean(form.keep_awake),
    })
    emit('saved', data)
  } catch (exc) {
    error.value = apiErrorText(exc)
  } finally {
    saving.value = false
  }
}

function clearWindow() {
  form.window_start = ''
  form.window_end = ''
}
</script>

<template>
  <section class="settings-panel" aria-labelledby="download-settings-title" :aria-busy="saving">
    <h3 id="download-settings-title" class="sr-only">下载设置</h3>
    <div class="settings-grid">
      <label class="settings-field">
        <span>同时下载数</span>
        <input v-model.number="form.max_concurrent_downloads" type="number" min="1" max="16" step="1" />
      </label>
      <label class="settings-field">
        <span>总连接数</span>
        <input v-model.number="form.max_connections" type="number" min="2" max="128" step="1" />
      </label>
      <label class="settings-field">
        <span>总限速 MB/s</span>
        <input v-model.number="form.rate_limit_mbps" type="number" min="0" max="1000" step="0.5" />
      </label>
      <div class="settings-field settings-window" role="group" aria-label="下载时段">
        <span>下载时段</span>
        <div class="settings-window-inputs">
          <input v-model="form.window_start" type="time" aria-label="开始时间" />
          <span aria-hidden="true">–</span>
          <input v-model="form.window_end" type="time" aria-label="结束时间" />
          <button v-if="form.window_start || form.window_end" type="button"
                  class="btn ghost sm" @click="clearWindow">全天</button>
        </div>
      </div>
    </div>
    <label class="notify-check settings-check">
      <input v-model="form.keep_awake" type="checkbox" />
      有任务时阻止电脑自动睡眠（不影响熄屏）
    </label>
    <ul class="settings-notes">
      <li>实际并发 {{ effective }}：每个任务至少占 2 条连接，取“同时下载数”和“连接数 ÷ 2”中较小的一个。</li>
      <li>限速 0 表示不限；按并发任务与分片线程平均分配，是近似值。</li>
      <li>时段都留空表示全天；时段外不开始新下载，正在下载的会下完，可跨午夜（如 22:00–08:00）。</li>
    </ul>
    <p v-if="error" class="settings-error" role="alert">{{ error }}</p>
    <div class="settings-actions">
      <button class="btn" :class="{ 'notify-save-dirty': dirty }" :disabled="saving" @click="save">
        {{ saving ? '保存中…' : '保存' }}
      </button>
      <button class="btn secondary" :disabled="saving" @click="emit('close')">收起</button>
      <span v-if="dirty" class="notify-unsaved" role="status">未保存</span>
    </div>
  </section>
</template>
