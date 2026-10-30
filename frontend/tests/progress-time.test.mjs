import test from 'node:test'
import assert from 'node:assert/strict'
import { progressTime } from '../src/utils/progressTime.js'

test('UTC 遥测与 SQLite 无时区快照按同一时刻比较，未知时间不猜测', () => {
  const expected = Date.UTC(2026, 8, 13, 10, 0, 4)
  for (const value of ['2026-09-13T10:00:04', '2026-09-13T10:00:04Z',
    '2026-09-13T10:00:04+00:00', '2026-09-13T18:00:04+08:00']) {
    assert.equal(progressTime(value), expected)
  }
  assert.ok(progressTime('2026-09-13T10:00:06') > progressTime('2026-09-13T10:00:04Z'))
  assert.ok(Number.isNaN(progressTime(null)))
  assert.ok(Number.isNaN(progressTime('invalid')))
})
