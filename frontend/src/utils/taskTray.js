/** 全局任务托盘的纯状态规则：REST 快照给计数，WebSocket 只合并进行中任务的遥测。 */
export const TRAY_TYPES = Object.freeze(['download', 'upload'])
// 上限略大于并发数（下载 5、上传 2），按状态排序后进行中任务总在最前。
export const TRAY_LIMITS = Object.freeze({ download: 6, upload: 3 })

const TELEMETRY_FIELDS = [
  'progress', 'speed_bps', 'downloaded_bytes', 'total_bytes', 'phase', 'last_progress_at',
]

export function emptyTrayGroup() {
  return { counts: {}, running: [], blocked: null }
}

function pickTask(task) {
  const picked = {
    id: task.id, title: task.title || task.viewkey || `任务 ${task.id}`,
    model_name: task.model_name || '', status: task.status, task_type: task.task_type,
  }
  for (const field of TELEMETRY_FIELDS) picked[field] = task[field]
  return picked
}

export function trayGroupFromResponse(data) {
  return {
    counts: { ...(data?.counts || {}) },
    running: (data?.tasks || []).filter(task => task.status === 'running').map(pickTask),
    // 后端环境门禁（如缺 ffmpeg/ffprobe）暂缓新下载时的原因。
    blocked: data?.download_blocked || null,
  }
}

/**
 * 合并一条 task_update。已跟踪的进行中任务只更新遥测；离开 running 或未跟踪
 * 任务出现状态/字节变化时返回 refresh，由调用方节流后读取带计数的快照。
 */
export function applyTrayPatch(group, patch) {
  const index = group.running.findIndex(task => task.id === patch.id)
  if (index < 0) {
    return { group, tracked: false, refresh: Boolean(patch.status) || 'last_progress_at' in patch }
  }
  if (patch.status && patch.status !== 'running') {
    return {
      group: { ...group, running: group.running.filter(task => task.id !== patch.id) },
      tracked: true,
      refresh: true,
    }
  }
  const running = [...group.running]
  const merged = { ...running[index] }
  for (const field of TELEMETRY_FIELDS) {
    if (field in patch) merged[field] = patch[field]
  }
  running[index] = merged
  return { group: { ...group, running }, tracked: true, refresh: false }
}

/**
 * 一次读取全部类型的进行中任务（status=running）后按类型拆分。计数是两类合计，
 * 放在下载组、上传组为空，traySummary 的逐类相加结果因此不变。
 */
export function splitTrayGroup(group) {
  const running = group?.running || []
  return {
    download: {
      counts: { ...(group?.counts || {}) },
      running: running.filter(task => task.task_type !== 'upload'),
      blocked: group?.blocked || null,
    },
    upload: {
      counts: {},
      running: running.filter(task => task.task_type === 'upload'),
      blocked: null,
    },
  }
}

export function traySummary(groups) {
  const count = (type, status) => Number(groups[type]?.counts?.[status] || 0)
  const speed = groups.download.running.reduce((sum, task) => {
    const value = Number(task.speed_bps)
    return Number.isFinite(value) && value > 0 ? sum + value : sum
  }, 0)
  // 进行中下载全部完成的粗略时间：取剩余最久的一个，不估算排队任务。
  const eta = groups.download.running.reduce((longest, task) => {
    const done = Number(task.downloaded_bytes)
    const size = Number(task.total_bytes)
    const rate = Number(task.speed_bps)
    if (task.phase !== 'downloading' || ![done, size, rate].every(Number.isFinite)
        || size <= 0 || rate <= 0 || done >= size) return longest
    return Math.max(longest, (size - done) / rate)
  }, 0)
  const summary = {
    downloading: groups.download.running.length,
    uploading: groups.upload.running.length,
    queued: count('download', 'queued') + count('upload', 'queued'),
    failed: count('download', 'failed') + count('upload', 'failed'),
    speed,
    eta,
    blocked: Boolean(groups.download.blocked),
    blockedCode: groups.download.blocked?.code || '',
  }
  // 下载时段是主动设置：没有排队任务时不因“时段外”常驻显示托盘。
  const blockingIdle = summary.blocked
    && !(summary.blockedCode === 'download_window' && !summary.queued)
  summary.idle = !summary.downloading && !summary.uploading && !summary.queued
    && !summary.failed && !blockingIdle
  return summary
}

export function trayPhaseText(task) {
  return {
    resolving: '解析地址',
    waiting: '等待数据',
    network_wait: '等待网络',
    processing: '封装校验',
    retrying: '重试中',
  }[task.phase] || ''
}
