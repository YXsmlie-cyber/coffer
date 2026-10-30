<script setup>
import { useRealtimeRefresh } from '../composables/useRealtimeRefresh'
import { computed, onActivated, onDeactivated, onUnmounted, ref, watch } from 'vue'
import api, { resolveApiAssetUrl, subscribeWS } from '../api'
import CoverCarousel from '../components/CoverCarousel.vue'
import PatrolBar from '../components/PatrolBar.vue'
import NotifyBar from '../components/NotifyBar.vue'
import VideoListModal from '../components/VideoListModal.vue'
import { confirm } from '../composables/useDialog'
import { apiErrorText as errorText } from '../utils/apiError'
import { formatDateTime } from '../utils/formatters'

defineOptions({ name: 'Library' })

const binding = ref('bound')
const items = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(36)
const query = ref('')
const loading = ref(true)
const refreshing = ref(false)
const busy = ref({})
const stale = ref(false)
const generation = ref(null)
const lastSuccessAt = ref(null)
const message = ref('')
const messageType = ref('')
const detailModel = ref(null)
let queryTimer = null
let pageActive = false
let loadDirty = true
let loadPromise = null
let loadController = null
let loadGeneration = 0

const pageCount = computed(() => Math.max(1, Math.ceil(total.value / pageSize.value)))

function notify(text, type = '') { message.value = text; messageType.value = type }
function avatar(model) { return resolveApiAssetUrl(model?.avatar_url) }
function dateText(value) {
  return formatDateTime(value, '尚未巡查')
}

async function load({ quiet = false, supersede = false } = {}) {
  loadDirty = true
  if (supersede) loadController?.abort()
  if (!pageActive) return false
  if (loadPromise) return loadPromise

  loadPromise = (async () => {
    let loaded = false
    while (loadDirty && pageActive) {
      loadDirty = false
      const requestGeneration = ++loadGeneration
      const controller = new AbortController()
      loadController = controller
      if (!quiet && !items.value.length) loading.value = true
      try {
        const { data } = await api.listLibrary({
          binding: binding.value, query: query.value, page: page.value, page_size: pageSize.value,
        }, { signal: controller.signal })
        if (requestGeneration !== loadGeneration || !pageActive) continue
        items.value = data.items || []
        total.value = data.total || 0
        stale.value = !!data.stale
        generation.value = data.scan_generation
        lastSuccessAt.value = data.last_success_at
        loaded = true
      } catch (error) {
        if (error.code !== 'ERR_CANCELED' && requestGeneration === loadGeneration) {
          notify('加载115模特失败：' + errorText(error), 'err')
        }
      } finally {
        if (requestGeneration === loadGeneration) loading.value = false
      }
    }
    return loaded
  })()
  try {
    return await loadPromise
  } finally {
    loadPromise = null
    loadController = null
    if (loadDirty && pageActive) void load({ quiet: true })
  }
}

async function refresh() {
  refreshing.value = true
  notify('正在读取VIDEO_HUB一级目录完整快照…')
  try {
    const { data } = await api.refreshLibrary()
    await load({ quiet: true })
    notify(`扫描完成：${data.bound_count} 个已绑定，${data.unbound_count} 个待绑定`)
  } catch (error) {
    notify('扫描失败，已保留上一次成功索引：' + errorText(error), 'err')
    await load({ quiet: true })
  } finally { refreshing.value = false }
}

async function bindFolder(item, modelId) {
  if (!modelId) return
  busy.value = { ...busy.value, [item.folder.cid]: true }
  try {
    await api.bindLibraryFolder(item.folder.cid, Number(modelId))
    notify(`已绑定目录“${item.folder.name}”`)
    await load({ quiet: true })
  } catch (error) { notify('绑定失败：' + errorText(error), 'err') }
  finally { busy.value = { ...busy.value, [item.folder.cid]: false } }
}

async function unbind(item) {
  if (!await confirm({
    title: '解除115目录绑定',
    message: `解除“${item.folder.name}”与“${item.model?.name}”的绑定？不会删除115文件、模特、视频或历史任务。`,
    danger: true, confirmText: '确认解除',
  })) return
  try {
    await api.unbindLibraryFolder(item.folder.cid)
    notify('已解除绑定，目录已移入待绑定')
    await load({ quiet: true })
  } catch (error) { notify('解除失败：' + errorText(error), 'err') }
}

async function sync(item) {
  const model = item.model
  if (!model) return
  busy.value = { ...busy.value, [item.folder.cid]: true }
  try {
    await api.syncModel(model.id)
    notify(`已完成 ${model.name} 的手工巡查`)
    await load({ quiet: true })
  } catch (error) { notify('巡查失败：' + errorText(error), 'err') }
  finally { busy.value = { ...busy.value, [item.folder.cid]: false } }
}

async function download(model) {
  try {
    const { data } = await api.addDownload(model.id, 'missing')
    notify(data.added ? `已添加 ${data.added} 个下载任务` : '没有新的明确缺失视频')
    await load({ quiet: true })
  } catch (error) { notify('添加下载失败：' + errorText(error), 'err') }
}

function changePage(next) { page.value = Math.min(Math.max(1, next), pageCount.value) }
function onWS(event) {
  if (event.type === 'model_update' && event.model) {
    const index = items.value.findIndex(item => item.model?.id === event.model.id)
    if (index >= 0) {
      items.value[index] = {
        ...items.value[index],
        model: { ...items.value[index].model, ...event.model },
      }
      if (loadPromise) loadDirty = true
    }
  } else if (event.type === 'library_membership_changed') {
    loadDirty = true
    void load({ quiet: true })
  } else if (event.type === 'library_refresh_progress') {
    refreshing.value = event.status === 'running'
    if (event.status !== 'running') void load({ quiet: true })
  }
}

watch([binding, page, pageSize], ([nextBinding, nextPage, nextSize], [oldBinding, oldPage, oldSize]) => {
  if ((nextBinding !== oldBinding || nextSize !== oldSize) && nextPage !== 1) {
    page.value = 1
    return
  }
  if (nextPage !== oldPage || nextBinding !== oldBinding || nextSize !== oldSize) {
    void load({ supersede: true })
  }
})
watch(query, () => {
  clearTimeout(queryTimer)
  queryTimer = setTimeout(() => {
    if (page.value !== 1) page.value = 1
    else void load({ supersede: true })
  }, 250)
})

let unsubscribe = null
onActivated(() => {
  pageActive = true
  unsubscribe ||= subscribeWS(onWS)
  void load()
})
onDeactivated(() => {
  pageActive = false
  loadDirty = true
  loadGeneration += 1
  loadController?.abort()
  unsubscribe?.()
  unsubscribe = null
})
onUnmounted(() => {
  pageActive = false
  clearTimeout(queryTimer)
  loadController?.abort()
  unsubscribe?.()
})

useRealtimeRefresh(() => load({ quiet: true }))
</script>

<template>
  <div class="page-title recommendation-title">
    <div>
      <h2>115模特</h2>
      <div class="sub">只管理VIDEO_HUB中已有模特：巡查、下载、绑定与通知</div>
    </div>
    <button class="btn" :disabled="refreshing" @click="refresh">
      {{ refreshing ? '扫描中…' : '刷新115目录' }}
    </button>
  </div>

  <div v-if="stale" class="status-banner warning" role="alert">
    当前115索引可能已过期；上次成功：{{ dateText(lastSuccessAt) }}。失败扫描没有覆盖旧索引。
  </div>

  <PatrolBar @feedback="event => notify(event.text, event.type)" />
  <NotifyBar @feedback="event => notify(event.text, event.type)" />

  <nav class="recommendation-tabs" aria-label="115目录绑定状态">
    <button :class="{ active: binding === 'bound' }" @click="binding = 'bound'">已绑定</button>
    <button :class="{ active: binding === 'unbound' }" @click="binding = 'unbound'">待绑定</button>
    <button :class="{ active: binding === 'error' }" @click="binding = 'error'">异常</button>
  </nav>

  <section class="candidate-tools">
    <input v-model="query" type="search" placeholder="搜索115目录名" aria-label="搜索115目录名" />
    <label>每页
      <select v-model.number="pageSize"><option :value="36">36</option><option :value="72">72</option></select>
    </label>
    <span>索引代次 {{ generation || '未扫描' }} · 共 {{ total }} 个目录</span>
  </section>

  <div class="toast-region" aria-live="polite">
    <div v-if="message" class="toast" :class="messageType"
         :role="messageType === 'err' ? 'alert' : 'status'">{{ message }}</div>
  </div>

  <div v-if="loading" class="discovery-grid">
    <div v-for="index in 6" :key="index" class="skeleton discovery-skeleton"></div>
  </div>
  <div v-else-if="!items.length" class="empty">当前筛选没有目录</div>
  <div v-else class="discovery-grid library-grid">
    <article v-for="item in items" :key="item.folder.cid" class="discovery-card library-card">
      <template v-if="item.model">
        <div class="discovery-card-head">
          <span class="creator-type">{{ item.folder.match_method }}</span>
          <span class="badge-sync synced">115已有</span>
        </div>
        <CoverCarousel :videos="item.preview_videos" :name="item.model.name"
                       :fallback-url="item.model.url" :identity="item.model.id" />
        <div class="discovery-creator">
          <a :href="item.model.url" target="_blank" rel="noopener noreferrer" class="discovery-avatar">
            <img v-if="avatar(item.model)" :src="avatar(item.model)" :alt="`${item.model.name} 的头像`"
                 loading="lazy" decoding="async" width="52" height="52" />
            <span v-else>模</span>
          </a>
          <div class="discovery-identity">
            <a :href="item.model.url" target="_blank" rel="noopener noreferrer" class="name">{{ item.model.name }}</a>
            <span class="meta-line" :title="item.folder.cid">115 / {{ item.folder.name }}</span>
          </div>
        </div>
        <div class="stats">
          <div class="stat"><b>{{ item.model.total_videos }}</b><label>作品</label></div>
          <div class="stat v-exists"><b>{{ item.model.cloud_videos }}</b><label>115已有</label></div>
          <div class="stat v-maybe"><b>{{ item.model.maybe_videos }}</b><label>疑似</label></div>
          <div class="stat v-missing"><b>{{ item.model.missing_videos }}</b><label>缺失</label></div>
          <div class="stat"><b>{{ item.model.pending_upload_videos }}</b><label>待上传</label></div>
        </div>
        <p class="candidate-note">最近巡查：{{ dateText(item.model.synced_at) }}</p>
        <div class="actions">
          <button class="btn" :disabled="busy[item.folder.cid]" @click="sync(item)">手工巡查</button>
          <button class="btn secondary" :disabled="!item.model.downloadable_videos"
                  @click="download(item.model)">下载缺失 {{ item.model.downloadable_videos || 0 }}</button>
          <button class="btn secondary" :disabled="!item.model.synced_at"
                  @click="detailModel = item.model">选择视频</button>
          <button class="btn danger" @click="unbind(item)">解除绑定</button>
        </div>
      </template>
      <template v-else>
        <div class="unbound-folder-icon" aria-hidden="true">115</div>
        <h3>{{ item.folder.name }}</h3>
        <p class="meta-line">CID：{{ item.folder.cid }}</p>
        <p v-if="item.folder.error" class="library-error">{{ item.folder.error }}</p>
        <label v-if="item.binding_suggestions?.length">
          选择匹配模特
          <select :disabled="busy[item.folder.cid]"
                  @change="bindFolder(item, $event.target.value); $event.target.value = ''">
            <option value="">请选择（不会自动绑定）</option>
            <option v-for="suggestion in item.binding_suggestions" :key="suggestion.model_id"
                    :value="suggestion.model_id">
              {{ suggestion.name }} · {{ Math.round(suggestion.confidence * 100) }}%
            </option>
          </select>
        </label>
        <p v-else class="candidate-note">暂无可靠建议，请先在模特页手工导入或确认身份。</p>
      </template>
    </article>
  </div>

  <div v-if="total > pageSize" class="pagination">
    <button class="btn secondary" :disabled="page <= 1" @click="changePage(page - 1)">上一页</button>
    <span>第 {{ page }} / {{ pageCount }} 页</span>
    <button class="btn secondary" :disabled="page >= pageCount" @click="changePage(page + 1)">下一页</button>
  </div>

  <VideoListModal v-if="detailModel" :model="detailModel"
                  @close="detailModel = null" @queued="load({ quiet: true })" />
</template>
