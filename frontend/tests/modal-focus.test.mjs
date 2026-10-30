import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import test from 'node:test'
import assert from 'node:assert/strict'
import { flush } from './component-harness.mjs'

function focusHarness() {
  const listeners = new Set()
  const mounts = []
  const unmounts = []
  let openWatcher
  const document = {
    activeElement: null,
    dialogs: [],
    addEventListener: (_, listener) => listeners.add(listener),
    removeEventListener: (_, listener) => listeners.delete(listener),
    querySelectorAll() { return this.dialogs },
  }
  class Element {
    constructor(children = []) { this.children = children; this.isConnected = true }
    focus() { document.activeElement = this }
    getClientRects() { return [1] }
    querySelectorAll() { return this.children }
    contains(element) { return element === this || this.children.includes(element) }
  }
  const opener = new Element()
  const fallback = new Element()
  const first = new Element()
  const last = new Element()
  const modal = new Element([first, last])
  document.dialogs = [modal]
  document.activeElement = opener
  const context = {
    document, HTMLElement: Element,
    nextTick: async () => {},
    onMounted: callback => mounts.push(callback),
    onUnmounted: callback => unmounts.push(callback),
    watch(ref, callback, options) {
      openWatcher = value => { ref.value = value; return callback(value) }
      if (options.immediate) callback(ref.value)
      return () => { openWatcher = null }
    },
  }
  vm.createContext(context)
  for (const filename of ['utils/modalStack.js', 'composables/useModalFocus.js']) {
    const source = readFileSync(new URL(`../src/${filename}`, import.meta.url), 'utf8')
      .replace(/^import[^\n]*\n/gm, '').replace(/^export /gm, '')
    vm.runInContext(source, context)
  }
  context.useModalFocus({ value: modal }, { value: false }, {
    initialFocusRef: { value: first },
    restoreFocusRef: { value: fallback },
  })
  mounts.forEach(callback => callback())
  return {
    listeners, document, opener, fallback, first, last, modal,
    open: value => openWatcher(value),
    unmount: () => unmounts.forEach(callback => callback()),
    tab(shiftKey = false) {
      const event = { key: 'Tab', shiftKey, prevented: false, preventDefault() { this.prevented = true } }
      listeners.forEach(listener => listener(event))
      return event
    },
  }
}

test('关闭的封面大图不注册焦点监听，打开后约束 Tab 并归还焦点', async () => {
  const page = focusHarness()
  assert.equal(page.listeners.size, 0)
  assert.equal(page.document.activeElement, page.opener)
  page.open(true)
  await flush()
  assert.equal(page.listeners.size, 1)
  assert.equal(page.document.activeElement, page.first)
  page.last.focus()
  assert.equal(page.tab().prevented, true)
  assert.equal(page.document.activeElement, page.first)
  assert.equal(page.tab(true).prevented, true)
  assert.equal(page.document.activeElement, page.last)
  page.open(false)
  assert.equal(page.listeners.size, 0)
  assert.equal(page.document.activeElement, page.opener)
})

test('叠加确认框时底层弹窗不截获 Tab；卸载后没有残余监听', async () => {
  const page = focusHarness()
  page.open(true)
  await flush()
  page.document.dialogs.push({ getClientRects: () => [1] })
  page.last.focus()
  assert.equal(page.tab().prevented, false)
  page.unmount()
  assert.equal(page.listeners.size, 0)
})

test('同一 tick 内打开再关闭，不在 nextTick 后重新安装焦点监听', async () => {
  const page = focusHarness()
  page.open(true)
  page.open(false)
  await flush()
  assert.equal(page.listeners.size, 0)
  assert.equal(page.document.activeElement, page.opener)
})

test('翻页替换触发按钮后，关闭恢复稳定容器且卸载不会再次抢焦点', async () => {
  const page = focusHarness()
  page.open(true)
  await flush()
  page.opener.isConnected = false
  page.open(false)
  assert.equal(page.document.activeElement, page.fallback)
  assert.equal(page.listeners.size, 0)
  page.last.focus()
  page.unmount()
  assert.equal(page.document.activeElement, page.last)
})

test('原触发按钮和回退容器都已移除时不尝试聚焦失效节点', async () => {
  const page = focusHarness()
  page.open(true)
  await flush()
  page.opener.isConnected = false
  page.fallback.isConnected = false
  page.open(false)
  assert.equal(page.document.activeElement, page.first)
  assert.equal(page.listeners.size, 0)
})
