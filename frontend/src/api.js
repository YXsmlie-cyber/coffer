import api from './api/http'
import { TASK_STATUS_ORDER } from './contracts/taskState'
export { resolveApiAssetUrl } from './utils/assetUrl'
export { subscribeWS, subscribeWSState, subscribeWSRefresh, pauseWSForAuth, resumeWSAfterAuth } from './api/websocket'

export default {
  // 外网应用内登录（账号密码仅随这一请求交给 Nginx htpasswd 校验）
  remoteAuthStatus: () => api.get('/remote-auth/status'),
  remoteLogin: (username, password, remember = true) => api.post(
    '/remote-auth/login', { remember }, { auth: { username, password } }),
  remoteLogout: () => api.post('/remote-auth/logout'),
  // 115
  cloudStatus: () => api.get('/115/status'),
  loginCookie: (cookie) => api.post('/115/login/cookie', { cookie }),
  qrcodeStart: () => api.post('/115/login/qrcode'),
  qrcodePoll: (uid, time, sign) => api.post('/115/login/qrcode/poll', { uid, time, sign }),
  // 模特 / 爬取
  scrapeRanking: (url, max_models = 100) => api.post('/scrape/ranking', { url, max_models }),
  discoverySources: () => api.get('/discovery/sources'),
  scrapeDiscovery: (source, max_creators = 50) => api.post(
    '/discovery/scrape', { source, max_creators }),
  listDiscovery: (source, config = {}) => api.get('/discovery', {
    ...config, params: { ...config.params, source },
  }),
  // 审美推荐（按 115 收藏本机计算，不触发任务）
  tasteModelState: (config = {}) => api.get('/taste/model', config),
  startTasteModelRun: () => api.post('/taste/model/run'),
  recordTasteResultFeedback: body => api.post('/taste/model/feedback', body),
  tasteLikedModels: (config = {}) => api.get('/taste/liked', config),
  lookupModel: (query) => api.post('/models/lookup', { query }),
  syncModel: (modelId) => api.post(`/scrape/model/${modelId}/sync`),
  createModelSyncJob: (model_ids, config = {}) => api.post(
    '/model-sync/jobs', { model_ids }, config),
  modelSyncJob: (jobId, config = {}) => api.get(`/model-sync/jobs/${jobId}`, config),
  listModels: (options = {}, config = {}) => api.get('/models', {
    ...config, params: { ...config.params, ...options },
  }),
  modelVideos: (modelId, config = {}) => api.get(`/models/${modelId}/videos`, config),
  deleteModel: (modelId) => api.delete(`/models/${modelId}`),
  // 115模特权威目录索引
  listLibrary: (options = {}, config = {}) => api.get('/library/models', {
    ...config, params: { ...config.params, ...options },
  }),
  refreshLibrary: () => api.post('/library/refresh'),
  bindLibraryFolder: (cid, model_id) => api.put(
    `/library/folders/${encodeURIComponent(cid)}/binding`, { model_id }),
  unbindLibraryFolder: (cid) => api.delete(
    `/library/folders/${encodeURIComponent(cid)}/binding`),
  // 下载
  addDownload: (model_id, mode, viewkeys = []) => api.post('/download', { model_id, mode, viewkeys }),
  // 任务
  listTasks: (type, options = {}, config = {}) => api.get('/tasks', {
    ...config, params: { ...config.params, type, ...options },
  }),
  startAll: (type) => api.post('/tasks/start-all', null, { params: { type } }),
  pauseAll: (type) => api.post('/tasks/pause-all', null, { params: { type } }),
  resumeAll: (type) => api.post('/tasks/resume-all', null, { params: { type } }),
  deleteTask: (id) => api.delete(`/tasks/${id}`),
  deleteTasks: (task_ids) => api.post('/tasks/delete-batch', { task_ids }),
  deleteAllTasks: (type) => api.post('/tasks/delete-all', null, { params: { type } }),
  retryTask: (id) => api.post(`/tasks/${id}/retry`),
  retryFailed: (type) => api.post('/tasks/retry-failed', null, { params: { type } }),
  // 下载运行参数（并发/连接/限速/时段/防睡眠）
  downloadSettings: (config = {}) => api.get('/settings/download', config),
  saveDownloadSettings: (body) => api.post('/settings/download', body),
  // 自动巡查
  patrolStatus: (config = {}) => api.get('/patrol/status', config),
  patrolToggle: (enabled) => api.post('/patrol/toggle', { enabled }),
  patrolConfig: (interval_hours) => api.post('/patrol/config', { interval_hours }),
  patrolRunNow: () => api.post('/patrol/run-now'),
  // Bark 通知推送
  notifyStatus: () => api.get('/notify/status'),
  notifyConfig: (cfg) => api.post('/notify/config', cfg),
  notifyTest: () => api.post('/notify/test'),
}

export function sortTasks(tasks) {
  return [...tasks].sort((a, b) => {
    const oa = TASK_STATUS_ORDER[a.status] ?? 6
    const ob = TASK_STATUS_ORDER[b.status] ?? 6
    if (oa !== ob) return oa - ob
    return b.id - a.id
  })
}
