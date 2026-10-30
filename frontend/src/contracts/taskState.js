// 与 backend/task_contracts.py 对照验收，变更须通过契约测试。
export const TASK_STATUS_ORDER = Object.freeze({
  "running": 0,
  "queued": 1,
  "paused": 2,
  "completed": 3,
  "failed": 4,
  "skipped": 5
})
