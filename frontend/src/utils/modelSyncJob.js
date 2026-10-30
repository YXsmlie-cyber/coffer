import api from '../api'

const TERMINAL_STATES = new Set(['completed', 'failed'])
const RETRYABLE_POLL_STATUSES = new Set([408, 425, 429])
const MAX_POLL_BACKOFF_MS = 10000

function abortError() {
  return new DOMException('页面已关闭，停止等待后台同步状态', 'AbortError')
}

function delay(ms, signal) {
  if (signal?.aborted) return Promise.reject(abortError())
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      signal?.removeEventListener('abort', onAbort)
      resolve()
    }, ms)
    function onAbort() {
      clearTimeout(timer)
      reject(abortError())
    }
    signal?.addEventListener('abort', onAbort, { once: true })
  })
}

export function isRetryableModelSyncPollError(error) {
  const status = Number(error?.response?.status || 0)
  // Axios 的 Network Error 没有 response；反向隧道短暂重连、Nginx 5xx 和限流
  // 都不能终止仍在后端运行的批量任务。
  if (!status) return true
  return status >= 500 || RETRYABLE_POLL_STATUSES.has(status)
}

/**
 * 创建后端批量同步任务并用状态接口兜底等待。
 * WebSocket 由页面负责即时更新；这里的轮询保证断线重连期间不会丢失最终结果。
 */
export async function runModelSyncJob(modelIds, {
  onCreated,
  onProgress,
  onRefresh,
  onConnectionState,
  shouldPoll = () => true,
  signal,
  pollMs = 1000,
  apiClient = api,
} = {}) {
  const uniqueIds = [...new Set(modelIds.map(Number).filter(
    value => Number.isInteger(value) && value > 0))]
  if (!uniqueIds.length) return { completed: 0, failed: [], job: null }

  if (signal?.aborted) throw abortError()
  const { data: created } = await apiClient.createModelSyncJob(uniqueIds, { signal })
  onCreated?.(created)
  let status = created
  let pollFailures = 0

  while (!TERMINAL_STATES.has(status.state)) {
    if (!shouldPoll()) {
      await delay(Math.max(500, pollMs), signal)
      continue
    }
    const waitMs = pollFailures
      ? Math.min(
        MAX_POLL_BACKOFF_MS,
        Math.max(pollMs, pollMs * (2 ** Math.min(pollFailures, 4))),
      )
      : pollMs
    await delay(waitMs, signal)
    try {
      const response = await apiClient.modelSyncJob(created.job_id, { signal })
      status = response.data
    } catch (error) {
      if (error?.response?.status === 404) {
        throw new Error('同步因服务重启中断；已完成的数据已保留，请重新发起同步')
      }
      if (!isRetryableModelSyncPollError(error)) throw error
      pollFailures += 1
      onConnectionState?.({
        state: 'retrying',
        attempt: pollFailures,
        error,
      })
      continue
    }
    if (pollFailures) {
      onConnectionState?.({
        state: 'recovered',
        attempts: pollFailures,
        status,
      })
      pollFailures = 0
    }
    onProgress?.(status)
  }

  onProgress?.(status)
  // 中间进度由 job/model_update 增量合并；只在终态做一次权威快照校准。
  await onRefresh?.(status)
  if (status.state === 'failed') {
    throw new Error(status.failure_reason || '批量同步任务失败')
  }
  return {
    completed: Number(status.succeeded || 0),
    failed: Array.isArray(status.errors) ? status.errors : [],
    job: status,
  }
}
