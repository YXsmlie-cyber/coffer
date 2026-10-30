/** 审美探索页的纯展示规则：批次序号、命中率、分组翻页与状态文案。 */

/** 页面每组显示多少位；后端一次给出整批（最多 96 位），“换一批”在本地翻页。 */
export const PAGE_SIZE = 24

/** 已完成批次的符合率柱；当前批次还在评价中，不计入。批次序号由后端给出。 */
export function hitRateBars(history, publishedRunId) {
  const current = Number(publishedRunId) || Infinity
  return (history || [])
    .filter(item => Number(item.run_id) < current && Number(item.rated) > 0)
    .map(item => ({
      key: item.run_id,
      label: item.batch ? `第 ${item.batch} 批` : '更早',
      rate: Math.max(0, Math.min(1, Number(item.like_rate) || 0)),
      liked: Number(item.liked) || 0,
      rated: Number(item.rated) || 0,
    }))
}

/** 累计评价小结，例如“累计评价 190 位，其中 86 位符合（45%）”。 */
export function ratingSummary(history) {
  const liked = (history || []).reduce((sum, item) => sum + (Number(item.liked) || 0), 0)
  const rated = (history || []).reduce((sum, item) => sum + (Number(item.rated) || 0), 0)
  if (!rated) return ''
  return `累计评价 ${rated} 位，其中 ${liked} 位符合（${Math.round(liked / rated * 100)}%）`
}

/** 最近两批与更早几批的平均对比，给出“在变好 / 持平 / 在变差”的直白判断。 */
export function trendText(bars) {
  if (!bars || bars.length < 4) return ''
  const average = list => list.reduce((sum, bar) => sum + bar.rate, 0) / list.length
  const recent = average(bars.slice(-2))
  const earlier = average(bars.slice(0, -2))
  const delta = recent - earlier
  if (delta >= 0.08) return '最近两批比之前更准'
  if (delta <= -0.08) return '最近两批不如之前'
  return '最近几批的准确度基本持平'
}

/** 第 page 组（从 0 开始）的结果与总组数；越界时回到第一组。 */
export function pageSlice(results, page, size = PAGE_SIZE) {
  const list = results || []
  const pageCount = Math.max(1, Math.ceil(list.length / size))
  const current = Number.isInteger(page) && page >= 0 && page < pageCount ? page : 0
  return {
    page: current,
    pageCount,
    items: list.slice(current * size, current * size + size),
  }
}

/** 用历史评价检验收藏模式的一句话说明；没有足够评价时说明尚未检验。 */
export function validationText(formal) {
  if (!formal) return ''
  if (!formal.validated) {
    return `你评价过的模特还不够（每类至少 ${formal.min_per_class || 8} 位），暂时无法检验准确度`
  }
  const top = Math.round(Number(formal.top30_like_rate || 0) * 100)
  const base = Math.round(Number(formal.base_like_rate || 0) * 100)
  return `用你过去评价过的 ${formal.labeled_models} 位检验：按这个方法排在前 30% 的人里，`
    + `你标“符合”的占 ${top}%（整体是 ${base}%）`
}

const FAILED = new Set(['failed', 'interrupted'])

/** 尚无可展示结果时的状态卡文案。formal 为最近一次生成的检验结果。 */
export function modelStatusCopy(status, formal = null) {
  if (status === 'quality_failed') {
    const auc = Number(formal?.auc)
    return {
      kicker: '暂不展示',
      title: '这次的推荐还不够准',
      body: `用你过去评价过的 ${formal?.labeled_models || 0} 位检验，区分度只有 `
        + `${Number.isFinite(auc) ? auc.toFixed(2) : '—'}（需要 0.65）。多收藏几位后会自动重新计算。`,
      action: '重新生成',
    }
  }
  if (FAILED.has(status)) {
    return {
      kicker: '没有完成',
      title: '这次生成没有完成',
      body: '已有的推荐不受影响；常见原因是网络中断或候选封面暂时取不到，重新生成即可。',
      action: '重新生成',
    }
  }
  return {
    kicker: '准备中',
    title: '正在准备第一批推荐',
    body: '会根据你 115 里收藏的模特自动挑人，不需要比较或评价，通常一分钟内完成。',
    action: '开始生成',
  }
}
