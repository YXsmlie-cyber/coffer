import test from 'node:test'
import assert from 'node:assert/strict'
import {
  applyTrayPatch, emptyTrayGroup, trayGroupFromResponse, traySummary,
} from '../src/utils/taskTray.js'
import { coverSources, modelState } from '../src/utils/modelMedia.js'

const snapshot = trayGroupFromResponse({
  tasks: [
    { id: 1, status: 'running', title: '甲', model_name: 'A', progress: 10, speed_bps: 2048, phase: 'downloading' },
    { id: 2, status: 'queued', title: '乙' },
  ],
  counts: { running: 1, queued: 7, failed: 2 },
})

test('托盘快照只跟踪进行中任务，计数沿用后端聚合', () => {
  assert.deepEqual(snapshot.running.map(task => task.id), [1])
  assert.equal(snapshot.counts.queued, 7)
  assert.deepEqual(trayGroupFromResponse(null), emptyTrayGroup())
  assert.deepEqual(trayGroupFromResponse({ download_blocked: { code: 'media_tools_unavailable' } }).blocked,
    { code: 'media_tools_unavailable' })
})

test('进行中任务的遥测只就地合并，不触发带计数的补读', () => {
  const result = applyTrayPatch(snapshot, {
    id: 1, progress: 55, speed_bps: 4096, title: '不应覆盖', last_progress_at: '2026-10-09T10:00:00Z',
  })
  assert.equal(result.refresh, false)
  assert.equal(result.tracked, true)
  assert.equal(result.group.running[0].progress, 55)
  assert.equal(result.group.running[0].title, '甲')
  assert.equal(snapshot.running[0].progress, 10, '原快照保持不可变')
  assert.equal(applyTrayPatch(snapshot, { id: 1, status: 'running', progress: 60 }).refresh, false)
})

test('离开 running 或未跟踪任务出现状态/字节变化时才要求补读', () => {
  const finished = applyTrayPatch(snapshot, { id: 1, status: 'completed', progress: 100 })
  assert.equal(finished.refresh, true)
  assert.deepEqual(finished.group.running, [])
  assert.equal(applyTrayPatch(snapshot, { id: 9, status: 'queued' }).refresh, true)
  assert.equal(applyTrayPatch(snapshot, { id: 9, last_progress_at: '2026-10-09T10:00:00Z' }).refresh, true)
  const quiet = applyTrayPatch(snapshot, { id: 9, error: '' })
  assert.equal(quiet.refresh, false)
  assert.equal(quiet.group, snapshot)
})

test('托盘汇总只累加有效速度，全部为零时隐藏', () => {
  const groups = {
    download: { counts: { queued: 3, failed: 1 }, running: [{ speed_bps: 1024 }, { speed_bps: -5 }, { speed_bps: null }] },
    upload: { counts: { queued: 2 }, running: [{}] },
  }
  assert.deepEqual(traySummary(groups), {
    downloading: 3, uploading: 1, queued: 5, failed: 1, speed: 1024, eta: 0, blocked: false,
    blockedCode: '', idle: false,
  })
  assert.equal(traySummary({ download: emptyTrayGroup(), upload: emptyTrayGroup() }).idle, true)
  // 环境门禁即使没有排队数也要显示，提醒补上 ffmpeg/ffprobe。
  const blocked = traySummary({
    download: { ...emptyTrayGroup(), blocked: { message: '找不到 ffprobe' } }, upload: emptyTrayGroup(),
  })
  assert.equal(blocked.blocked, true)
  assert.equal(blocked.idle, false)
})

test('卡片角标优先级：同步中 > 未同步 > 缺失 > 待上传 > 已齐', () => {
  const synced = { synced_at: '2026-10-08', missing_videos: 3, pending_upload_videos: 2 }
  assert.equal(modelState(synced, true).tone, 'syncing')
  assert.equal(modelState({ ...synced, synced_at: null }).tone, 'idle')
  assert.deepEqual(modelState(synced), { tone: 'missing', text: '缺 3' })
  assert.deepEqual(modelState({ ...synced, missing_videos: 0 }), { tone: 'pending', text: '待上传 2' })
  assert.equal(modelState({ synced_at: '2026-10-08' }).tone, 'done')
})

test('封面优先本机清晰图，失败后回退源站缩略图，且只取首个作品', () => {
  const model = { preview_videos: [
    { preview_image_url: '/api/previews/a.webp', thumbnail: 'https://cdn.example/a.jpg' },
    { preview_image_url: '/api/previews/b.webp' },
  ] }
  assert.deepEqual(coverSources(model), ['/api/previews/a.webp', 'https://cdn.example/a.jpg'])
  assert.deepEqual(coverSources({ preview_videos: [{ preview_image_url: 'file:///x', thumbnail: '' }] }), [])
  assert.deepEqual(coverSources({}), [])
})
