import test from 'node:test'
import assert from 'node:assert/strict'
import { componentHarness } from './component-harness.mjs'

test('Bark 草稿提示不发送请求，只有原保存动作才持久化', async () => {
  const calls = []
  const page = componentHarness('components/NotifyBar.vue', {
    api: {
      notifyStatus: async () => ({ data: { enabled: false, configured: true } }),
      notifyConfig: async body => {
        calls.push(body)
        return { data: { enabled: body.enabled, configured: true, events: body.events } }
      },
    },
  }, ['loadStatus', 'enabled', 'events', 'barkUrl', 'dirty', 'save'])
  await page.state.loadStatus()
  assert.equal(page.state.dirty.value, false)
  page.state.enabled.value = true
  assert.equal(page.state.dirty.value, true)
  assert.equal(calls.length, 0)
  await page.state.save()
  assert.equal(calls.length, 1)
  assert.equal(calls[0].enabled, true)
  assert.equal(page.state.dirty.value, false)
  page.state.events.download_done = true
  assert.equal(page.state.dirty.value, true)
  page.state.events.download_done = false
  assert.equal(page.state.dirty.value, false)
  page.state.barkUrl.value = 'https://example.invalid/mock-key'
  assert.equal(page.state.dirty.value, true)
  assert.equal(calls.length, 1)
})

test('Bark 保存失败保留草稿和未保存状态', async () => {
  const page = componentHarness('components/NotifyBar.vue', {
    api: {
      notifyStatus: async () => ({ data: { enabled: false } }),
      notifyConfig: async () => { throw new Error('保存失败') },
    },
  }, ['loadStatus', 'enabled', 'dirty', 'save'])
  await page.state.loadStatus()
  page.state.enabled.value = true
  await page.state.save()
  assert.equal(page.state.enabled.value, true)
  assert.equal(page.state.dirty.value, true)
})
