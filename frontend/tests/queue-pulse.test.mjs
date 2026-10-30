import test from 'node:test'
import assert from 'node:assert/strict'
import { etaText, formatDuration, untilText } from '../src/utils/formatters.js'
import { faviconHref, faviconSvg, pulseState, pulseTitle, BASE_TITLE } from '../src/utils/documentPulse.js'
import { TASK_VIEWS, taskMatchesView } from '../src/utils/taskViews.js'
import { splitTrayGroup, trayGroupFromResponse, traySummary } from '../src/utils/taskTray.js'

test('粗略时长：分钟取整，长时间带小时与天，无效值为空', () => {
  assert.equal(formatDuration(0), '')
  assert.equal(formatDuration(Number.NaN), '')
  assert.equal(formatDuration(42), '不到 1 分钟')
  assert.equal(formatDuration(170), '约 3 分钟')
  assert.equal(formatDuration(3600), '约 1 小时')
  assert.equal(formatDuration(4800), '约 1 小时 20 分')
  assert.equal(formatDuration(3 * 86400), '约 3 天')
})

test('剩余时间只在总量、速度与进度都有效时给出', () => {
  assert.equal(etaText(400, 1000, 10), '约 1 分钟')
  assert.equal(etaText(0, 0, 10), '')
  assert.equal(etaText(100, 1000, 0), '')
  assert.equal(etaText(1000, 1000, 10), '')
  assert.equal(etaText(null, 1000, 10), '约 2 分钟')
})

test('复查倒计时按本地时钟计算，到期显示“即将”', () => {
  const now = Date.parse('2026-10-19T20:00:00')
  assert.equal(untilText('2026-10-19 20:14:10', now), '约 14 分钟后')
  assert.equal(untilText('2026-10-19T20:00:10', now), '即将')
  assert.equal(untilText('2026-10-19T19:00:00', now), '即将')
  assert.equal(untilText('', now), '')
  assert.equal(untilText('not-a-date', now), '')
})

test('标签页标题：下载速度、上传与失败依次出现，空闲恢复原标题', () => {
  assert.equal(pulseTitle({}), BASE_TITLE)
  assert.equal(pulseTitle({ downloading: 3, speed: 12.4 * 1024 ** 2 }),
    `↓3 · 12 MB/s — ${BASE_TITLE}`)
  assert.equal(pulseTitle({ downloading: 1, uploading: 2, failed: 1 }),
    `↓1 · ↑2 · ⚠ 1 失败 — ${BASE_TITLE}`)
  assert.equal(pulseTitle({}, true), `✓ 队列已清空 — ${BASE_TITLE}`)
  // 仍有运行任务时“已清空”不出现。
  assert.equal(pulseTitle({ downloading: 1 }, true), `↓1 — ${BASE_TITLE}`)
})

test('图标状态：失败优先于运行，清空只在空闲时出现', () => {
  assert.equal(pulseState({ failed: 1, downloading: 2 }), 'failed')
  assert.equal(pulseState({ uploading: 1 }), 'running')
  assert.equal(pulseState({}, true), 'cleared')
  assert.equal(pulseState({}), 'idle')
  assert.doesNotMatch(faviconSvg('idle'), /circle/)
  assert.match(faviconSvg('failed'), /#ff3b30/)
  assert.ok(faviconHref('running').startsWith('data:image/svg+xml,'))
})

test('筛选视图：活动视图含失败，失败分类视图只看 failed，全部不过滤', () => {
  assert.equal(TASK_VIEWS.active.status, 'running,queued,paused,failed')
  assert.ok(taskMatchesView({ status: 'failed' }, 'active'))
  assert.ok(!taskMatchesView({ status: 'completed' }, 'active'))
  assert.ok(taskMatchesView({ status: 'failed' }, 'failed_manual'))
  assert.ok(!taskMatchesView({ status: 'queued' }, 'failed_recoverable'))
  assert.ok(taskMatchesView({ status: 'skipped' }, 'all'))
  assert.ok(taskMatchesView({ status: 'running' }, 'unknown-view'))
})

test('托盘整体剩余时间取进行中下载里最慢的一个', () => {
  const summary = traySummary({
    download: {
      counts: {},
      running: [
        { phase: 'downloading', downloaded_bytes: 0, total_bytes: 600, speed_bps: 10 },
        { phase: 'downloading', downloaded_bytes: 0, total_bytes: 600, speed_bps: 1 },
        { phase: 'resolving', downloaded_bytes: 0, total_bytes: 6000, speed_bps: 1 },
      ],
    },
    upload: { counts: {}, running: [] },
  })
  assert.equal(summary.eta, 600)
})

test('一次读取的进行中任务按类型拆分，合计计数不重复累加', () => {
  const group = trayGroupFromResponse({
    tasks: [
      { id: 1, status: 'running', task_type: 'download', speed_bps: 100 },
      { id: 2, status: 'running', task_type: 'upload' },
      { id: 3, status: 'running', task_type: 'download', speed_bps: 50 },
    ],
    counts: { running: 3, queued: 4, failed: 1 },
    download_blocked: null,
  })
  const groups = splitTrayGroup(group)
  assert.deepEqual(groups.download.running.map(task => task.id), [1, 3])
  assert.deepEqual(groups.upload.running.map(task => task.id), [2])
  const summary = traySummary(groups)
  assert.equal(summary.downloading, 2)
  assert.equal(summary.uploading, 1)
  assert.equal(summary.queued, 4)
  assert.equal(summary.failed, 1)
  assert.equal(summary.speed, 150)
})

test('下载时段外但没有排队任务时托盘保持空闲，有排队时提示等待时段', () => {
  const windowBlocked = { code: 'download_window', message: '当前不在下载时段 22:00–08:00' }
  const empty = traySummary({
    download: { counts: {}, running: [], blocked: windowBlocked },
    upload: { counts: {}, running: [] },
  })
  assert.equal(empty.blockedCode, 'download_window')
  assert.equal(empty.idle, true)
  const waiting = traySummary({
    download: { counts: { queued: 3 }, running: [], blocked: windowBlocked },
    upload: { counts: {}, running: [] },
  })
  assert.equal(waiting.idle, false)
  const outage = traySummary({
    download: { counts: {}, running: [], blocked: { code: 'network_outage' } },
    upload: { counts: {}, running: [] },
  })
  assert.equal(outage.idle, false, '网络中断即使没有排队也要提示')
})

test('速度曲线坐标：按最大值归一，全零贴底，单点画水平线', async () => {
  const { sparklinePoints } = await import('../src/utils/sparkline.js')
  assert.equal(sparklinePoints([]), '')
  assert.equal(sparklinePoints([0, 10], 120, 24, 2), '2.0,22.0 118.0,2.0')
  assert.equal(sparklinePoints([0, 0, 0], 120, 24, 2), '2.0,22.0 60.0,22.0 118.0,22.0')
  assert.equal(sparklinePoints([5], 120, 24, 2), '2.0,2.0 118.0,2.0')
  assert.equal(sparklinePoints([NaN, -3, 4], 120, 24, 2).split(' ')[0], '2.0,22.0')
})
