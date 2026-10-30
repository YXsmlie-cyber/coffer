import test from 'node:test'
import assert from 'node:assert/strict'
import { SEQUENCE_TIMEOUT_MS, SHORTCUT_HELP, shortcutAction } from '../src/utils/shortcuts.js'

const key = (value, extra = {}) => ({ key: value, target: { tagName: 'BODY' }, ...extra })

test('g + 字母在时限内切换页面，超时或未知字母不动作', () => {
  const first = shortcutAction(key('g'), null, 1000)
  assert.equal(first.action, null)
  assert.deepEqual(shortcutAction(key('d'), first.pending, 1500).action, { go: '/downloads' })
  assert.deepEqual(shortcutAction(key('U'), first.pending, 1500).action, { go: '/uploads' })
  assert.equal(shortcutAction(key('d'), first.pending, 1000 + SEQUENCE_TIMEOUT_MS + 1).action, null)
  assert.equal(shortcutAction(key('x'), first.pending, 1500).action, null)
})

test('/ 聚焦搜索、? 打开说明；输入框内或带修饰键时不处理', () => {
  assert.equal(shortcutAction(key('/')).action, 'focus-search')
  assert.equal(shortcutAction(key('?')).action, 'help')
  assert.equal(shortcutAction(key('/', { target: { tagName: 'INPUT' } })).action, null)
  assert.equal(shortcutAction(key('/', { target: { isContentEditable: true } })).action, null)
  assert.equal(shortcutAction(key('g', { ctrlKey: true })).pending, null)
  assert.equal(shortcutAction(key('/', { defaultPrevented: true })).action, null)
  assert.ok(SHORTCUT_HELP.some(([keys]) => keys === 'g d'))
})
