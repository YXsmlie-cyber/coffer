const FIELD_LABELS = {
  max_models: '最多模特数',
  interval_hours: '巡查间隔',
  query: '搜索内容',
  url: '榜单 URL',
}
function validationItemText(item) {
  if (typeof item === 'string') return item
  if (!item || typeof item !== 'object') return String(item || '')
  const fieldKey = Array.isArray(item.loc) ? item.loc[item.loc.length - 1] : ''
  const field = FIELD_LABELS[fieldKey] || fieldKey || ''
  let message = item.msg || item.message || ''
  if (item.type === 'less_than_equal') message = `不能大于 ${item.ctx?.le}`
  else if (item.type === 'greater_than_equal') message = `不能小于 ${item.ctx?.ge}`
  else if (item.type === 'int_parsing') message = '必须是整数'
  else if (item.type === 'missing') message = '不能为空'
  else if (item.type === 'string_too_short') message = '内容过短'
  else if (item.type === 'string_too_long') message = '内容过长'
  return field && message ? `${field}${message}` : (message || field)
}

/** 把 Axios/FastAPI 错误统一转换为可直接展示的中文文本。 */
export function apiErrorText(error) {
  const detail = error?.response?.data?.detail
  if (typeof detail === 'string' && detail.trim()) return detail
  if (Array.isArray(detail)) {
    const messages = detail.map(validationItemText).filter(Boolean)
    if (messages.length) return messages.join('；')
  }
  if (detail && typeof detail === 'object') {
    const message = detail.message || detail.msg
    if (message) return String(message)
    try { return JSON.stringify(detail) } catch (_) { /* 使用通用回退 */ }
  }
  return error?.response?.data?.message || error?.message || '未知错误'
}
