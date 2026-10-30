import { apiPathPrefix } from '../utils/assetUrl.js'

/** 一个应用一个连接；传入环境使重连/鉴权行为能独立验证。 */
export function createRealtimeClient({ window, document, navigator, location, WebSocket, random = Math.random }) {
  let socket = null
  let authBlocked = false
  let attempt = 0
  let timer = null
  let connectedBefore = false
  let disposed = false
  let state = { status: navigator.onLine === false ? 'offline' : 'idle', connectedAt: null }
  const messages = new Set()
  const changes = new Set()
  const refreshes = new Set()
  const deliver = (listeners, value) => {
    for (const listener of listeners) { try { listener(value) } catch (_) {} }
  }
  function publish(status) {
    if (state.status === status) return
    state = { ...state, status }
    deliver(changes, { ...state })
  }
  function clearTimer() {
    if (timer !== null) window.clearTimeout(timer)
    timer = null
  }
  function canConnect() {
    return !disposed && !authBlocked && messages.size > 0
      && navigator.onLine !== false && !document.hidden
  }
  function requestRefresh(reason) {
    if (!disposed && !authBlocked && !document.hidden) deliver(refreshes, { reason })
  }
  function scheduleReconnect() {
    if (!canConnect() || socket || timer !== null) return
    publish('reconnecting')
    const base = Math.min(30000, 1000 * (2 ** Math.min(attempt++, 5)))
    timer = window.setTimeout(() => { timer = null; ensure() }, base + Math.round(base * random() * 0.3))
  }
  function ensure() {
    if (socket || !canConnect()) return
    publish(connectedBefore ? 'reconnecting' : 'connecting')
    const proto = location.protocol === 'https:' ? 'wss' : 'ws'
    const ws = new WebSocket(`${proto}://${location.host}${apiPathPrefix(location.pathname)}/ws`)
    socket = ws
    ws.onopen = () => {
      if (socket !== ws || disposed) return
      attempt = 0
      clearTimer()
      state = { ...state, connectedAt: new Date().toISOString() }
      publish('connected')
      // 首次握手也补齐 REST 快照与连接建立之间的消息空窗。
      requestRefresh(connectedBefore ? 'reconnect' : 'connect')
      connectedBefore = true
    }
    ws.onmessage = event => {
      if (socket !== ws || disposed || authBlocked) return
      let message
      try { message = JSON.parse(event.data) } catch (_) { return }
      deliver(messages, message)
    }
    ws.onclose = event => {
      // 已退出登录/离线的旧连接迟到 close 不能干扰新连接或重新阻断鉴权。
      if (socket !== ws || disposed) return
      socket = null
      if (event.code === 4401) {
        pauseForAuth()
        window.dispatchEvent(new CustomEvent('ph115:remote-auth-required'))
        return
      }
      publish(navigator.onLine === false ? 'offline' : 'reconnecting')
      scheduleReconnect()
    }
  }
  function detachSocket(reason) {
    const ws = socket
    socket = null
    if (ws && ws.readyState < WebSocket.CLOSING) ws.close(1000, reason)
  }
  function pauseForAuth() {
    authBlocked = true
    clearTimer()
    detachSocket('Signing out')
    publish('auth_required')
  }
  function resumeAfterAuth() {
    authBlocked = false
    attempt = 0
    ensure()
  }
  function online() { attempt = 0; ensure() }
  function offline() { clearTimer(); detachSocket('Offline'); publish('offline') }
  function visible() {
    if (document.hidden) return
    attempt = 0
    if (socket?.readyState === WebSocket.OPEN) requestRefresh('visible')
    ensure()
  }
  window.addEventListener('online', online)
  window.addEventListener('offline', offline)
  document.addEventListener('visibilitychange', visible)
  return {
    subscribe(listener) {
      messages.add(listener)
      ensure()
      return () => messages.delete(listener)
    },
    subscribeState(listener) { changes.add(listener); listener({ ...state }); return () => changes.delete(listener) },
    subscribeRefresh(listener) { refreshes.add(listener); return () => refreshes.delete(listener) },
    pauseForAuth,
    resumeAfterAuth,
    dispose() {
      disposed = true
      clearTimer()
      detachSocket('Disposed')
      window.removeEventListener('online', online)
      window.removeEventListener('offline', offline)
      document.removeEventListener('visibilitychange', visible)
      messages.clear(); changes.clear(); refreshes.clear()
    },
  }
}

let client = null
function shared() {
  return client ||= createRealtimeClient({ window, document, navigator, location, WebSocket })
}
export const subscribeWS = listener => shared().subscribe(listener)
export const subscribeWSState = listener => shared().subscribeState(listener)
export const subscribeWSRefresh = listener => shared().subscribeRefresh(listener)
export const pauseWSForAuth = () => shared().pauseForAuth()
export const resumeWSAfterAuth = () => shared().resumeAfterAuth()
if (import.meta.hot) import.meta.hot.dispose(() => { client?.dispose(); client = null })
