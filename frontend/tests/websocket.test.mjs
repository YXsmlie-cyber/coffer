import test from 'node:test'
import assert from 'node:assert/strict'
import { createRealtimeClient } from '../src/api/websocket.js'

function fixture() {
  const events = new EventTarget()
  const document = new EventTarget()
  document.hidden = false
  const timers = new Map()
  let serial = 0
  const navigator = { onLine: true }
  class Socket {
    static OPEN = 1
    static CLOSING = 2
    static all = []
    constructor(url) { this.url = url; this.readyState = 0; Socket.all.push(this) }
    open() { this.readyState = 1; this.onopen?.() }
    close(code = 1006) { this.readyState = 3; this.onclose?.({ code }) }
    message(value) { this.onmessage?.({ data: JSON.stringify(value) }) }
  }
  const window = {
    addEventListener: events.addEventListener.bind(events),
    removeEventListener: events.removeEventListener.bind(events),
    dispatchEvent: events.dispatchEvent.bind(events),
    setTimeout(fn, delay) { const id = ++serial; timers.set(id, { fn, delay }); return id },
    clearTimeout(id) { timers.delete(id) },
  }
  const client = createRealtimeClient({ window, document, navigator, WebSocket: Socket,
    location: { protocol: 'https:', host: 'example.test', pathname: '/ph115/' }, random: () => 0 })
  return { client, Socket, document, navigator, timers,
    event: name => events.dispatchEvent(new Event(name)),
    timer() { const [id, task] = timers.entries().next().value; timers.delete(id); task.fn() },
  }
}

test('同一应用只创建一个连接；取消全部订阅不关闭连接，连接后通知补读', () => {
  const f = fixture()
  const refreshes = []
  f.client.subscribeRefresh(event => refreshes.push(event.reason))
  const unsubscribe = f.client.subscribe(() => {})
  const second = f.client.subscribe(() => {})
  assert.equal(f.Socket.all.length, 1)
  assert.equal(f.Socket.all[0].url, 'wss://example.test/ph115/ws')
  f.Socket.all[0].open()
  assert.deepEqual(refreshes, ['connect'])
  unsubscribe(); second()
  assert.equal(f.Socket.all[0].readyState, 1)
  f.client.dispose()
})

test('断线期间的状态由一次恢复通知补读，后台返回也通知当前订阅者', () => {
  const f = fixture()
  const refreshes = []
  f.client.subscribe(() => {})
  f.client.subscribeRefresh(event => refreshes.push(event.reason))
  f.Socket.all[0].open()
  f.Socket.all[0].close()
  assert.equal(f.timers.size, 1)
  f.timer()
  f.Socket.all[1].open()
  f.document.hidden = false
  f.document.dispatchEvent(new Event('visibilitychange'))
  assert.deepEqual(refreshes, ['connect', 'reconnect', 'visible'])
  assert.equal(f.timers.size, 0)
  f.client.dispose()
})

test('鉴权阻断停止重连，旧连接迟到关闭和消息不影响恢复后的连接', () => {
  const f = fixture()
  const messages = []
  f.client.subscribe(message => messages.push(message))
  const old = f.Socket.all[0]
  old.open()
  f.client.pauseForAuth()
  f.event('online')
  assert.equal(f.Socket.all.length, 1)
  f.client.resumeAfterAuth()
  const current = f.Socket.all[1]
  current.open()
  old.onclose({ code: 4401 })
  old.message({ stale: true })
  current.message({ current: true })
  assert.deepEqual(messages, [{ current: true }])
  assert.equal(current.readyState, 1)
  assert.equal(f.timers.size, 0)
  f.client.dispose()
})

test('离线或隐藏时不创建重连；销毁后没有残留事件和计时器', () => {
  const f = fixture()
  f.client.subscribe(() => {})
  f.Socket.all[0].open()
  f.navigator.onLine = false
  f.event('offline')
  assert.equal(f.timers.size, 0)
  f.navigator.onLine = true
  f.document.hidden = true
  f.event('online')
  assert.equal(f.Socket.all.length, 1)
  f.document.hidden = false
  f.document.dispatchEvent(new Event('visibilitychange'))
  assert.equal(f.Socket.all.length, 2)
  f.client.dispose()
  f.event('online')
  f.document.dispatchEvent(new Event('visibilitychange'))
  assert.equal(f.Socket.all.length, 2)
})
