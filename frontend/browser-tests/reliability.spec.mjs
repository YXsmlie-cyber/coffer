import { test, expect } from '@playwright/test'

async function fixture(page) {
  const sockets = []
  const reads = []
  const writes = []
  const state = { status: 'running' }
  const task = () => ({
    id: 99, viewkey: 'example', model_id: 1, title: '测试任务 · 长标题与状态校准',
    model_name: '示例', task_type: 'download', status: state.status,
    progress: state.status === 'completed' ? 100 : 25, phase: 'downloading',
    downloaded_bytes: state.downloadedBytes ?? 1024, total_bytes: 4096, speed_bps: 512,
    last_progress_at: state.progressAt || '2026-09-13T10:00:00Z',
    error: '', bulk_recoverable: false,
    ...state.override,
  })
  await page.route(url => /^\/(?:ph115\/)?api\//.test(url.pathname), async route => {
    const request = route.request()
    const url = new URL(request.url())
    const path = url.pathname.replace(/^\/ph115/, '')
    if (request.method() !== 'GET') {
      writes.push(path)
      await route.fulfill({ status: 405, json: { detail: '只读浏览器夹具' } })
      return
    }
    reads.push(path)
    let body = {}
    if (path === '/api/remote-auth/status') body = { required: false, authenticated: true, remember_days: 30 }
    else if (path === '/api/115/status') body = { logged_in: false }
    else if (path === '/api/tasks') body = {
      tasks: [task()], total: 1, next_cursor: null,
      counts: {
        running: state.status === 'running' ? 1 : 0,
        queued: state.status === 'queued' ? 1 : 0,
        completed: state.status === 'completed' ? 1 : 0,
        failed: state.status === 'failed' ? 1 : 0,
        failed_manual: state.status === 'failed' ? 1 : 0,
      },
    }
    else if (path === '/api/models') body = { models: [], total: 0, next_cursor: null, has_more: false }
    else if (path === '/api/library/models') body = { items: [], total: 0, page: 1, page_size: 72, counts: {} }
    else if (path === '/api/discovery/sources') body = { sources: [{ key: 'home', label: '示例来源' }] }
    else if (path === '/api/discovery') body = { creators: [], fetched_at: null }
    else if (path === '/api/taste/pair') body = { pair: null, stats: { goal_remaining: 20, total: 0 } }
    else if (path === '/api/taste/model') body = { status: 'not_started', results: [], run: null }
    else if (path === '/api/taste/liked') body = { models: [], summary: {} }
    else if (path === '/api/patrol/status') body = { enabled: false, running: false, interval_hours: 6 }
    else if (path === '/api/notify/status') body = { enabled: false, configured: false }
    else return route.fulfill({ status: 404, json: { detail: '未定义的隔离接口' } })
    if (path === '/api/tasks' && state.holdNextRead) {
      const wait = state.holdNextRead
      state.holdNextRead = null
      await wait
    }
    await route.fulfill({ json: body })
  })
  await page.routeWebSocket('**/ws', socket => { sockets.push(socket) })
  return { state, sockets, reads, writes, task }
}

test('来源冷却与额度耗尽在桌面和手机显示清楚且行高稳定', async ({ page }, info) => {
  const f = await fixture(page)
  f.state.status = 'queued'
  f.state.override = { phase: 'source_cooldown', next_retry_at: '2026-09-19T01:00:00', auto_retry_count: 2 }
  await page.goto('/#/downloads')
  const row = page.locator('.task-row')
  await expect(row).toContainText('等待源恢复')
  await expect(row).toContainText('复查 · 2/3')
  expect(await row.evaluate(el => el.getBoundingClientRect().height)).toBe(62)
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await page.screenshot({ path: info.outputPath('source-cooldown.png'), fullPage: true })
  await expect(page.locator('.connection-status')).toHaveText('实时已连接')
  f.state.status = 'failed'
  f.state.override = { phase: 'failed_source_exhausted', next_retry_at: null, auto_retry_count: 3 }
  f.sockets[0].send(JSON.stringify({ type: 'task_update', task: f.task() }))
  await expect(row).toContainText('来源需处理')
  await expect(row).toContainText('自动复查已停止')
  await expect(row.getByRole('button', { name: '重试', exact: true })).toBeVisible()
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await page.screenshot({ path: info.outputPath('source-exhausted.png'), fullPage: true })
  expect(f.writes).toEqual([])
})

test('断线期间完成的任务在重连后自动校准', async ({ page }) => {
  const f = await fixture(page)
  await page.goto('/#/downloads')
  await expect(page.locator('.task-row')).toContainText('进行中')
  await expect(page.locator('.connection-status')).toHaveText('实时已连接')
  await expect.poll(() => f.reads.filter(path => path === '/api/tasks').length).toBeGreaterThanOrEqual(2)
  f.state.status = 'completed'
  f.sockets[0].close({ code: 1012, reason: 'fixture reconnect' })
  await expect(page.locator('.connection-status')).toHaveText('实时重连中')
  await expect(page.locator('.task-row')).toContainText('已完成', { timeout: 8000 })
  await expect.poll(() => f.sockets.length).toBe(2)
  expect(f.reads.filter(path => path === '/api/tasks').length).toBeLessThanOrEqual(4)
  expect(f.writes).toEqual([])
})

test('六个路由切换保持单例连接和手机无横向溢出', async ({ page }) => {
  const f = await fixture(page)
  const errors = []
  page.on('pageerror', error => errors.push(error.message))
  await page.goto('/#/models')
  await expect(page.locator('.connection-status')).toHaveText('实时已连接')
  for (const path of ['library', 'discovery', 'taste', 'downloads', 'uploads', 'models']) {
    await page.locator(`.nav a[href="#/${path}"]`).click()
    await expect(page.locator('main#main-content')).toBeVisible()
    await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  }
  expect(f.sockets).toHaveLength(1)
  expect(errors).toEqual([])
  expect(f.writes).toEqual([])
})

test('任务进度更新保留选择，取消删除保留任务与焦点', async ({ page }) => {
  const f = await fixture(page)
  await page.goto('/#/downloads')
  const selected = page.getByRole('checkbox', { name: '选择任务 测试任务 · 长标题与状态校准' })
  await selected.check()
  f.sockets[0].send(JSON.stringify({ type: 'task_update', task: {
    id: 99, task_type: 'download', progress: 40, downloaded_bytes: 1600,
  } }))
  await expect(selected).toBeChecked()
  const button = page.getByRole('button', { name: '删除已选 (1)' })
  await button.click()
  const dialog = page.getByRole('alertdialog')
  await expect(dialog).toContainText('删除已选 1 个下载任务')
  await page.keyboard.press('Escape')
  await expect(dialog).toBeHidden()
  await expect(button).toBeFocused()
  await expect(page.locator('.task-row')).toHaveCount(1)
  expect(await page.locator('.task-row').evaluate(element => element.getBoundingClientRect().height)).toBe(62)
  expect(f.writes).toEqual([])
})

test('过期会话立即回到登录页并停止重连', async ({ page }) => {
  const f = await fixture(page)
  await page.goto('/#/downloads')
  await expect(page.locator('.connection-status')).toHaveText('实时已连接')
  f.sockets[0].close({ code: 4401, reason: 'fixture expired' })
  await expect(page.locator('#remote-username')).toBeVisible()
  await page.waitForTimeout(1400)
  expect(f.sockets).toHaveLength(1)
  expect(f.writes).toEqual([])
})

test('刷新时较旧的 SQLite 字节快照不覆盖实时遥测，完成状态仍以快照为准', async ({ page }) => {
  const f = await fixture(page)
  await page.goto('/#/downloads')
  const progress = page.getByRole('progressbar', { includeHidden: true })
  await expect(progress).toHaveAttribute('aria-valuenow', '25')
  f.sockets[0].send(JSON.stringify({ type: 'task_update', task: {
    id: 99, task_type: 'download', status: 'running', downloaded_bytes: 2048,
    total_bytes: 4096, last_progress_at: '2026-09-13T10:00:04Z',
  } }))
  await expect(progress).toHaveAttribute('aria-valuenow', '50')
  await page.getByRole('button', { name: '刷新', exact: true }).click()
  await expect(page.locator('[aria-labelledby="download-queue-title"]')).toHaveAttribute('aria-busy', 'false')
  await expect(progress).toHaveAttribute('aria-valuenow', '50')
  f.state.downloadedBytes = 3072
  f.state.progressAt = '2026-09-13T10:00:06'
  await page.getByRole('button', { name: '刷新', exact: true }).click()
  await expect(progress).toHaveAttribute('aria-valuenow', '75')
  await expect(page.locator('.progress-telemetry')).toHaveAttribute('title', /18:00:06/)
  f.state.status = 'completed'
  await page.getByRole('button', { name: '刷新', exact: true }).click()
  await expect(page.locator('.task-row')).toContainText('已完成')
  await expect(progress).toHaveAttribute('aria-valuenow', '100')
})

test('大量进度遇到挂起读取时重新校准，迟到旧快照不覆盖终态', async ({ page }) => {
  const f = await fixture(page)
  await page.goto('/#/downloads')
  await expect(page.locator('.task-row')).toContainText('进行中')
  await expect.poll(() => f.reads.filter(path => path === '/api/tasks').length).toBeGreaterThanOrEqual(2)
  let release
  f.state.holdNextRead = new Promise(resolve => { release = resolve })
  const before = f.reads.filter(path => path === '/api/tasks').length
  await page.getByRole('button', { name: '刷新', exact: true }).click()
  await expect.poll(() => f.reads.filter(path => path === '/api/tasks').length).toBeGreaterThan(before)
  f.state.status = 'completed'
  for (let index = 0; index < 2200; index += 1) {
    f.sockets[0].send(JSON.stringify({ type: 'task_update', task: {
      id: 99, task_type: 'download', downloaded_bytes: index,
    } }))
  }
  try {
    await expect(page.locator('.task-row')).toContainText('已完成', { timeout: 8000 })
  } finally {
    release()
  }
  await page.unrouteAll({ behavior: 'wait' })
  await expect(page.locator('.task-row')).toContainText('已完成')
  expect(f.writes).toEqual([])
})
