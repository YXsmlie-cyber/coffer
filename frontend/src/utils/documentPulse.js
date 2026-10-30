/** 标签页标题与图标的纯规则：后台标签也能看出下载进度、失败与队列清空。 */
import { formatSpeed } from './formatters.js'

export const BASE_TITLE = 'PH · 115'

/** idle / running / failed / cleared；失败优先，提醒有事需要处理。 */
export function pulseState(summary, cleared = false) {
  if (summary?.failed) return 'failed'
  if (summary?.downloading || summary?.uploading) return 'running'
  return cleared ? 'cleared' : 'idle'
}

export function pulseTitle(summary, cleared = false) {
  const parts = []
  if (summary?.downloading) {
    const speed = formatSpeed(summary.speed)
    parts.push(`↓${summary.downloading}${speed ? ` · ${speed}` : ''}`)
  }
  if (summary?.uploading) parts.push(`↑${summary.uploading}`)
  if (summary?.failed) parts.push(`⚠ ${summary.failed} 失败`)
  if (!parts.length && cleared) parts.push('✓ 队列已清空')
  return parts.length ? `${parts.join(' · ')} — ${BASE_TITLE}` : BASE_TITLE
}

// 图标：Apple 蓝圆角方块里一个下载箭头；右上角状态点（运行绿、失败红、清空绿勾）。
const DOT = {
  running: '<circle cx="25" cy="7" r="6" fill="#34c759" stroke="#fff" stroke-width="2"/>',
  failed: '<circle cx="25" cy="7" r="6" fill="#ff3b30" stroke="#fff" stroke-width="2"/>',
  cleared: '<circle cx="25" cy="7" r="6" fill="#34c759" stroke="#fff" stroke-width="2"/>'
    + '<path d="M22.4 7.2l1.8 1.8 3.4-3.6" fill="none" stroke="#fff" stroke-width="1.8"'
    + ' stroke-linecap="round" stroke-linejoin="round"/>',
  idle: '',
}

export function faviconSvg(state = 'idle') {
  return '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
    + '<rect x="2" y="4" width="26" height="26" rx="7" fill="#0071e3"/>'
    + '<path d="M15 10v10m0 0-4.5-4.5M15 20l4.5-4.5M10 24h10" fill="none" stroke="#fff"'
    + ' stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/>'
    + (DOT[state] || '')
    + '</svg>'
}

export function faviconHref(state = 'idle') {
  return `data:image/svg+xml,${encodeURIComponent(faviconSvg(state))}`
}
