const likedDateFormatter = new Intl.DateTimeFormat('zh-CN', {
  month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
})

const dateTimeFormatter = new Intl.DateTimeFormat('zh-CN', {
  dateStyle: 'short', timeStyle: 'medium', hour12: false,
})

export function formatLikedDate(value, fallback = '已记录') {
  if (!value) return fallback
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? fallback : likedDateFormatter.format(date)
}

export function formatDateTime(value, fallback = '') {
  if (!value) return fallback
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? fallback : dateTimeFormatter.format(date)
}

export function formatBytes(bytes) {
  const value = Number(bytes)
  if (!Number.isFinite(value) || value < 0) return ''
  if (value < 1024) return `${Math.round(value)} B`
  if (value < 1024 ** 2) return `${(value / 1024).toFixed(1)} KB`
  if (value < 1024 ** 3) return `${(value / 1024 ** 2).toFixed(1)} MB`
  return `${(value / 1024 ** 3).toFixed(2)} GB`
}

/** 把秒数格式化为“约 3 分钟”一类的粗略时长；无效或非正值返回空串。 */
export function formatDuration(seconds) {
  const value = Number(seconds)
  if (!Number.isFinite(value) || value <= 0) return ''
  if (value < 60) return '不到 1 分钟'
  const minutes = Math.round(value / 60)
  if (minutes < 60) return `约 ${minutes} 分钟`
  const hours = Math.floor(minutes / 60)
  if (hours >= 48) return `约 ${Math.round(hours / 24)} 天`
  const rest = minutes % 60
  return rest ? `约 ${hours} 小时 ${rest} 分` : `约 ${hours} 小时`
}

/** 剩余字节 ÷ 当前滚动速度；缺少总量或速度时返回空串。 */
export function etaText(downloaded, total, bytesPerSecond) {
  const done = Number(downloaded)
  const size = Number(total)
  const speed = Number(bytesPerSecond)
  if (![done, size, speed].every(Number.isFinite) || size <= 0 || speed <= 0 || done >= size) {
    return ''
  }
  return formatDuration((size - done) / speed)
}

/** 距离某个将来时刻的倒计时文字；已过期返回“即将”。 */
export function untilText(value, now = Date.now()) {
  if (!value) return ''
  const stamp = Date.parse(String(value).replace(' ', 'T'))
  if (!Number.isFinite(stamp)) return ''
  const seconds = (stamp - now) / 1000
  return seconds <= 30 ? '即将' : `${formatDuration(seconds)}后`
}

export function formatSpeed(bytesPerSecond) {
  const value = Number(bytesPerSecond)
  if (!Number.isFinite(value) || value <= 0) return ''
  if (value < 1024) return `${Math.round(value)} B/s`
  if (value < 1024 ** 2) {
    const kb = value / 1024
    return `${kb < 10 ? kb.toFixed(1) : Math.round(kb)} KB/s`
  }
  if (value < 1024 ** 3) {
    const mb = value / 1024 ** 2
    return `${mb < 10 ? mb.toFixed(1) : mb.toFixed(0)} MB/s`
  }
  const gb = value / 1024 ** 3
  return `${gb < 10 ? gb.toFixed(1) : gb.toFixed(0)} GB/s`
}
