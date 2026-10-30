import test from 'node:test'
import assert from 'node:assert/strict'
import { componentHarness, deferred, flush } from './component-harness.mjs'

const qr = { data: { qr_image: 'mock-image', uid: 'mock', time: 1, sign: 'mock' } }
function login(api) {
  return componentHarness('components/LoginModal.vue', { api }, ['switchTab', 'qrImage', 'qrTip'])
}

test('二维码请求迟到：卸载后不更新图片、不恢复轮询', async () => {
  const request = deferred()
  const page = login({ qrcodeStart: () => request.promise })
  page.mount()
  page.unmount()
  request.resolve(qr)
  await flush()
  assert.equal(page.state.qrImage.value, '')
  assert.equal(page.timers.size, 0)
  assert.equal(page.emitted.length, 0)
})

test('切换 Cookie 后，迟到二维码响应和错误均不覆盖当前界面', async () => {
  for (const fail of [false, true]) {
    const request = deferred()
    const page = login({ qrcodeStart: () => request.promise })
    page.mount()
    page.state.switchTab('cookie')
    const originalTip = page.state.qrTip.value
    fail ? request.reject(new Error('过期请求')) : request.resolve(qr)
    await flush()
    assert.equal(page.state.qrImage.value, '')
    assert.equal(page.state.qrTip.value, originalTip)
    assert.equal(page.timers.size, 0)
  }
})

test('快速切换扫码方式仅接受最新二维码', async () => {
  const first = deferred()
  const second = deferred()
  let calls = 0
  const page = login({ qrcodeStart: () => ++calls === 1 ? first.promise : second.promise })
  page.mount()
  page.state.switchTab('cookie')
  page.state.switchTab('qrcode')
  second.resolve({ data: { ...qr.data, qr_image: 'new-image' } })
  await flush()
  first.resolve(qr)
  await flush()
  assert.equal(page.state.qrImage.value, 'new-image')
  assert.equal(page.timers.size, 1)
  assert.equal([...page.timers.values()][0].delay, 1200)
})

test('迟到轮询：关闭后成功、扫描、过期、取消均不发事件或安排下一轮', async () => {
  for (const status of [1, 2, -1, -2]) {
    const request = deferred()
    const page = login({ qrcodeStart: async () => qr, qrcodePoll: () => request.promise })
    page.mount()
    await flush()
    const pending = page.runTimer()
    page.unmount()
    const originalTip = page.state.qrTip.value
    request.resolve({ data: { status } })
    await pending
    assert.equal(page.state.qrTip.value, originalTip)
    assert.equal(page.timers.size, 0)
    assert.equal(page.emitted.length, 0)
  }
})

test('正常扫码保持 1200/2000ms 轮询，并仅发一次登录成功', async () => {
  const statuses = [1, 2]
  const page = login({
    qrcodeStart: async () => qr,
    qrcodePoll: async () => ({ data: { status: statuses.shift() } }),
  })
  page.mount()
  await flush()
  assert.equal([...page.timers.values()][0].delay, 1200)
  await page.runTimer()
  assert.equal(page.state.qrTip.value, '已扫描，请在手机上确认')
  assert.equal([...page.timers.values()][0].delay, 2000)
  await page.runTimer()
  assert.equal(page.emitted.length, 1)
  assert.equal(page.emitted[0][0], 'logged-in')
  assert.equal(page.timers.size, 0)
})

test('当前二维码过期保持 800ms 重建；网络错误保持 2000ms 重试', async () => {
  for (const fail of [false, true]) {
    const page = login({
      qrcodeStart: async () => qr,
      qrcodePoll: async () => {
        if (fail) throw new Error('离线')
        return { data: { status: -1 } }
      },
    })
    page.mount()
    await flush()
    await page.runTimer()
    assert.equal([...page.timers.values()][0].delay, fail ? 2000 : 800)
    page.unmount()
    assert.equal(page.timers.size, 0)
  }
})
