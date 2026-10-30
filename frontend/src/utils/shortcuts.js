/** 全局键盘快捷键的纯规则：输入框内、带修饰键或有弹窗时不处理。 */
export const SEQUENCE_TIMEOUT_MS = 1200

export const GO_TARGETS = Object.freeze({
  m: { path: '/models', label: '模特' },
  l: { path: '/library', label: '115模特' },
  v: { path: '/discovery', label: '视频' },
  t: { path: '/taste', label: '探索' },
  d: { path: '/downloads', label: '下载' },
  u: { path: '/uploads', label: '上传' },
})

export const SHORTCUT_HELP = [
  ['/', '聚焦当前页的搜索框'],
  ...Object.entries(GO_TARGETS).map(([key, target]) => [`g ${key}`, `前往“${target.label}”`]),
  ['?', '显示这份快捷键说明'],
]

function isEditable(target) {
  if (!target || typeof target !== 'object') return false
  const tag = String(target.tagName || '').toUpperCase()
  return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || Boolean(target.isContentEditable)
}

/**
 * 根据按键与上一键（pending，含时间戳）给出动作。
 * 返回 { action, pending }：action 为 null / 'focus-search' / 'help' / { go: path }。
 */
export function shortcutAction(event, pending = null, now = Date.now()) {
  if (!event || event.defaultPrevented || event.ctrlKey || event.metaKey || event.altKey
      || isEditable(event.target)) {
    return { action: null, pending: null }
  }
  const key = String(event.key || '')
  if (pending && pending.key === 'g' && now - pending.at <= SEQUENCE_TIMEOUT_MS) {
    const target = GO_TARGETS[key.toLowerCase()]
    return { action: target ? { go: target.path } : null, pending: null }
  }
  if (key === 'g') return { action: null, pending: { key: 'g', at: now } }
  if (key === '/') return { action: 'focus-search', pending: null }
  if (key === '?') return { action: 'help', pending: null }
  return { action: null, pending: null }
}
