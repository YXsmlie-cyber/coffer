/** 原生下载时间源为 UTC；SQLite 的 DateTime 列读回时可能没有时区后缀。
 * 只用于 last_progress_at，不改变巡查/创建时间等本地时间的解释。
 */
export function progressTime(value) {
  if (typeof value !== 'string' || !value.trim()) return NaN
  const stamp = value.trim().replace(' ', 'T')
  return Date.parse(/(?:z|[+-]\d{2}:?\d{2})$/i.test(stamp) ? stamp : `${stamp}Z`)
}
