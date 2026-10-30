import test from 'node:test'
import assert from 'node:assert/strict'
import {
  hitRateBars, modelStatusCopy, pageSlice, ratingSummary, trendText, validationText,
} from '../src/utils/tasteView.js'

const history = [
  { run_id: 4, batch: 1, liked: 12, disliked: 12, rated: 24, like_rate: 0.5 },
  { run_id: 5, batch: 2, liked: 14, disliked: 10, rated: 24, like_rate: 0.5833 },
  { run_id: 6, batch: 3, liked: 4, disliked: 19, rated: 23, like_rate: 0.1739 },
  { run_id: 12, batch: 9, liked: 1, disliked: 0, rated: 1, like_rate: 1 },
]

test('命中率柱只算已完成的批次，序号来自后端', () => {
  const bars = hitRateBars(history, 12)
  assert.deepEqual(bars.map(bar => bar.label), ['第 1 批', '第 2 批', '第 3 批'])
  assert.equal(bars[2].rate, 0.1739)
  assert.equal(hitRateBars([], 12).length, 0)
  assert.equal(hitRateBars([{ run_id: 3, liked: 0, rated: 0 }], 12).length, 0)
})

test('累计小结与趋势判断', () => {
  assert.equal(ratingSummary(history.slice(0, 3)), '累计评价 71 位，其中 30 位符合（42%）')
  assert.equal(ratingSummary([]), '')
  const bar = rate => ({ rate })
  assert.equal(trendText([bar(0.3), bar(0.3), bar(0.5), bar(0.6)]), '最近两批比之前更准')
  assert.equal(trendText([bar(0.6), bar(0.6), bar(0.3), bar(0.3)]), '最近两批不如之前')
  assert.equal(trendText([bar(0.5), bar(0.5), bar(0.52), bar(0.48)]), '最近几批的准确度基本持平')
  assert.equal(trendText([bar(0.5), bar(0.5)]), '', '批次太少不下结论')
})

test('换一批：每组 24 位，越界回到第一组', () => {
  const results = Array.from({ length: 60 }, (_, index) => ({ rank: index + 1 }))
  const first = pageSlice(results, 0)
  assert.equal(first.pageCount, 3)
  assert.equal(first.items.length, 24)
  assert.equal(pageSlice(results, 2).items.length, 12)
  assert.equal(pageSlice(results, 2).items[0].rank, 49)
  assert.equal(pageSlice(results, 3).page, 0, '结果变少后越界的页码回到第一组')
  assert.deepEqual(pageSlice([], 0), { page: 0, pageCount: 1, items: [] })
})

test('检验说明：已检验给出前 30% 符合率，评价不足时直说', () => {
  assert.match(validationText({
    validated: true, labeled_models: 237, top30_like_rate: 0.5211, base_like_rate: 0.3924,
  }), /237 位.*52%.*39%/)
  assert.match(validationText({ validated: false, min_per_class: 8 }), /每类至少 8 位/)
  assert.equal(validationText(null), '')
})

test('状态卡文案覆盖准备中、质量不足与中断', () => {
  assert.equal(modelStatusCopy('not_started').action, '开始生成')
  assert.match(modelStatusCopy('not_started').body, /115/)
  const weak = modelStatusCopy('quality_failed', { auc: 0.6012, labeled_models: 120 })
  assert.match(weak.body, /120 位/)
  assert.match(weak.body, /0\.60/)
  assert.equal(weak.action, '重新生成')
  assert.match(modelStatusCopy('interrupted').title, /没有完成/)
})
