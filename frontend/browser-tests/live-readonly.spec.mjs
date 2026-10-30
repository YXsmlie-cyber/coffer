import { test, expect } from '@playwright/test'

// 仅显式启用时读取本机 API。图片/名称/标题替换为中性内容，写入全部拒绝。
test.skip(!process.env.PH115_READONLY_QA, '需要显式启用本机只读验收')

function neutral(value) {
  if (Array.isArray(value)) return value.map(neutral)
  if (!value || typeof value !== 'object') return value
  return Object.fromEntries(Object.entries(value).map(([key, item]) => {
    if (['name', 'model_name', 'title'].includes(key) && typeof item === 'string') {
      return [key, `示例 ${'长标题'.repeat(Math.min(12, Math.ceil(item.length / 3)))}`]
    }
    return [key, neutral(item)]
  }))
}

test('构建后的主要页面使用真实 API 结构与统计进行只读视觉验收', async ({ page }, info) => {
  const errors = []
  page.on('pageerror', error => errors.push(error.message))
  await page.route(url => url.pathname.startsWith('/api/'), async route => {
    if (route.request().method() !== 'GET') return route.fulfill({ status: 405, json: {} })
    const source = new URL(route.request().url())
    const result = await page.request.get(`http://127.0.0.1:8000${source.pathname}${source.search}`)
    let data
    try { data = neutral(await result.json()) } catch (_) { data = {} }
    await route.fulfill({ status: result.status(), json: data })
  })
  await page.route('**/*', async route => {
    if (route.request().resourceType() === 'image') return route.fulfill({
      contentType: 'image/svg+xml',
      body: '<svg xmlns="http://www.w3.org/2000/svg" width="640" height="360"><rect width="640" height="360" fill="#242934"/><circle cx="320" cy="150" r="50" fill="#475267"/><path d="M200 360V310a120 100 0 0 1 240 0v50" fill="#475267"/></svg>',
    })
    if (new URL(route.request().url()).hostname !== '127.0.0.1') return route.abort()
    return route.fallback()
  })
  await page.routeWebSocket('**/ws', () => {})
  await page.goto('/#/downloads')
  await expect(page.locator('.task-row').first()).toBeVisible()
  await page.screenshot({ path: info.outputPath('downloads.png'), fullPage: true })
  for (const [name, endpoint] of [
    ['models', '/api/models'], ['library', '/api/library/models'],
    ['discovery', '/api/discovery'], ['taste', '/api/taste/model'], ['uploads', '/api/tasks'],
  ]) {
    const ready = page.waitForResponse(response => new URL(response.url()).pathname === endpoint)
    await page.locator(`.nav a[href="#/${name}"]`).click()
    await ready
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))))
    await expect(page.locator('#main-content')).toBeVisible()
    await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
    await page.screenshot({ path: info.outputPath(`${name}.png`), fullPage: true })
  }
  expect(errors).toEqual([])
  await page.unrouteAll({ behavior: 'wait' })
})
