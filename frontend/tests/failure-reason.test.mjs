import test from 'node:test'
import assert from 'node:assert/strict'
import { failureDetail, failureReason } from '../src/utils/failureReason.js'

const reasonOf = (error, extra = {}) => failureReason({ status: 'failed', error, ...extra }).reason

test('真实错误文案翻译成对应原因', () => {
  assert.equal(reasonOf('NetworkDownloadError: \u001b[0;31mERROR:\u001b[0m [download] Got error: '
    + '[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol (_ssl.c:1082).'),
  '网络或代理链路中断')
  assert.equal(reasonOf('HLSManifestFetchError: HLS 清单请求失败: ConnectError'), '网络或代理链路中断')
  assert.equal(reasonOf('HLSSourceUnavailableError: HLS 清单请求返回 HTTP 410'),
    '分到了不可用的 CDN 节点（410）')
  // 同时含网络字样与 410 时以 410 为准。
  assert.equal(reasonOf('HTTP Error 410: Gone | previous connection timed out'),
    '分到了不可用的 CDN 节点（410）')
  assert.equal(reasonOf('DownloadError: yt-dlp 子进程异常退出(code=1)，未返回完成事件'),
    '下载进程启动后意外退出')
  assert.equal(reasonOf('DownloadEnvironmentError: 未找到 ffprobe'), '本机缺少 ffmpeg / ffprobe')
  assert.equal(reasonOf('Cloud115Error: 未登录115：找不到 cookie 文件'), '115 登录已失效')
  assert.equal(reasonOf('DownloadArtifactError: 下载 staging 的视频时间戳倒退'), '下载内容不完整或顺序错乱')
  assert.equal(reasonOf('sqlite3.OperationalError: database is locked'), '数据库短暂繁忙')
  // 同时含 403 连接字样时以存储桶策略为准，不能误报成代理断流。
  assert.equal(reasonOf('UploadTransferError: 上传失败: MultipartUploadAbort: 上传初始化或分片流程中止 '
    + '<- HTTPError: HTTP Error 403: Forbidden（fhnfile.oss-cn-shenzhen.aliyuncs.com；'
    + 'OSS AccessDenied：Access denied by bucket policy.；EC 0003-00000205） connection reset'),
  '115 拒绝了本机的上传请求（存储桶策略）')
})

test('跳过原因与来源耗尽有专门说明，未知错误回落到通用提示', () => {
  assert.equal(failureReason({ status: 'skipped', error: '115已存在(二次去重)' }).reason,
    '115 已有相同视频，已跳过上传')
  assert.equal(failureReason({ status: 'skipped', error: 'VideoUnavailableError: 视频已下架或不可播放：此视频已下架' }).reason,
    '源站已下架或不可播放')
  assert.equal(failureReason({ status: 'failed', phase: 'failed_source_exhausted', error: 'x' }).reason,
    'CDN 来源持续不可用，自动复查已用完')
  assert.equal(reasonOf('SomethingNew: 完全没见过的错误'), '未归类的错误')
  assert.equal(failureReason(null).reason, '未归类的错误')
})

test('弹窗正文先给建议，再附原始信息', () => {
  const detail = failureDetail({ status: 'failed', error: 'DownloadError: yt-dlp 子进程异常退出(code=1)' })
  assert.match(detail, /^崩溃详情记录在 data\/backend-stderr\.log/)
  assert.match(detail, /原始信息：\nDownloadError: yt-dlp 子进程异常退出\(code=1\)$/)
  assert.doesNotMatch(failureDetail({ status: 'failed', error: '' }), /原始信息/)
})

test('CDN 限流 429（含并发分片引发的 write to closed file）单独说明', () => {
  const tail = 'ERROR: [download] Got error: HTTP Error 429: Too Many Requests. Giving up after 3 retries'
  assert.equal(reasonOf(`NetworkDownloadError: ${tail}`), 'PH 视频 CDN 限流（429）')
  assert.equal(reasonOf(`DownloadError: write to closed file | ${tail}`), 'PH 视频 CDN 限流（429）')
  assert.equal(reasonOf('HLSManifestFetchError: HLS 清单请求返回 HTTP 429'), 'PH 视频 CDN 限流（429）')
})

test('地区限制的视频页单独说明，可换节点后重试', () => {
  assert.equal(
    failureReason({ status: 'skipped', error: 'VideoUnavailableError: 视频已下架或不可播放：此页面不可用（可能限制了当前出口地区）' }).reason,
    '视频页对当前代理出口不可用')
})
