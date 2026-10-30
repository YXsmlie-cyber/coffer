/** 同源地址工具；导入不创建 HTTP/WS 客户端或浏览器事件监听。 */
export function apiPathPrefix(pathname = globalThis.location?.pathname || '/') {
  return pathname === '/ph115' || pathname.startsWith('/ph115/') ? '/ph115' : ''
}

export function resolveApiAssetUrl(path) {
  const value = String(path || '').trim()
  return value.startsWith('/api/') ? `${apiPathPrefix()}${value}` : ''
}
