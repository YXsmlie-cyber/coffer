/** 任务队列的筛选视图；纯数据规则，供页面与测试共用。 */
// 统计胶囊即筛选视图。默认“活动”：运行、排队、暂停与失败，避免失败任务被
// 数万条完成记录压到列表末尾（服务端按 运行→排队→暂停→完成→失败 排序）。
export const TASK_VIEWS = Object.freeze({
  active: { status: 'running,queued,paused,failed' },
  running: { status: 'running' },
  queued: { status: 'queued' },
  paused: { status: 'paused' },
  completed: { status: 'completed' },
  failed: { status: 'failed' },
  failed_recoverable: { failure: 'recoverable' },
  failed_manual: { failure: 'manual' },
  skipped: { status: 'skipped' },
  all: {},
})

/** 本地判断任务是否仍属于当前视图；失败分类需服务端判断，交给随后的刷新。 */
export function taskMatchesView(task, view) {
  const filter = TASK_VIEWS[view] || {}
  if (filter.failure) return task.status === 'failed'
  if (!filter.status) return true
  return filter.status.split(',').includes(task.status)
}
