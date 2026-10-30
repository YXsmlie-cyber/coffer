import test from 'node:test'
import assert from 'node:assert/strict'
import { componentHarness } from './component-harness.mjs'

test('来源等待显示到期时间，耗尽后明确停止；暂停优先于等待提示', () => {
  const { state } = componentHarness('components/TaskTable.vue', {
    defineProps: () => ({ tasks: [] }), defineExpose() {}, watch() {},
    formatDateTime: value => value || '',
    untilText: value => (value ? '约 14 分钟后' : ''),
  }, ['taskStatusText', 'taskStatusTitle', 'taskNote'])
  const task = { status: 'queued', phase: 'source_cooldown', next_retry_at: '2026-09-19 01:00', auto_retry_count: 2 }
  assert.equal(state.taskStatusText(task), '等待源恢复')
  // 行内显示倒计时，具体时刻放在悬停说明里。
  assert.match(state.taskNote(task), /约 14 分钟后复查.*2\/3/)
  assert.match(state.taskStatusTitle(task), /^2026-09-19 01:00 复查/)
  assert.match(state.taskNote({ ...task, next_retry_at: null }), /等待调度.*2\/3/)
  assert.equal(state.taskStatusText({ ...task, status: 'paused' }), '已暂停')
  const exhausted = { status: 'failed', phase: 'failed_source_exhausted' }
  assert.match(state.taskNote(exhausted), /自动复查已停止/)
  assert.equal(state.taskStatusText(exhausted), '来源需处理')
  assert.match(state.taskStatusTitle({ status: 'failed', bulk_recoverable: true }), /不表示来源已经恢复/)
})
