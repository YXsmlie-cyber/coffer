/**
 * 把失败/跳过任务的原始错误翻译成一句原因和一个建议动作。规则自上而下取第一条
 * 命中；只做展示，不改变后端的重试分类（可重试与否仍以 bulk_recoverable 为准）。
 */
const RULES = [
  {
    when: task => task.phase === 'failed_source_exhausted',
    reason: 'CDN 来源持续不可用，自动复查已用完',
    action: '可以稍后手动重试一次；若仍然失败，视频可能已在源站失效。',
  },
  {
    test: /限制了当前出口地区/,
    reason: '视频页对当前代理出口不可用',
    action: '多半是上传者限制了地区（也可能已转私密或删除）；换到其他地区的节点后可以对它单独重试，否则无需处理。',
  },
  {
    test: /已下架|不可播放|视频页不存在/,
    reason: '源站已下架或不可播放',
    action: '无需处理，这个视频已经无法下载。',
  },
  {
    test: /115\s*已存在|二次去重/,
    reason: '115 已有相同视频，已跳过上传',
    action: '无需处理。',
  },
  {
    test: /子进程异常退出/,
    reason: '下载进程启动后意外退出',
    action: '崩溃详情记录在 data/backend-stderr.log；查看后可直接重试。',
  },
  {
    test: /(未找到|无法启动)\s*(ffmpeg|ffprobe)|等待本机 ffmpeg/i,
    reason: '本机缺少 ffmpeg / ffprobe',
    action: '把固定版本放到 tools/ffmpeg 或设置 FFMPEG_DIR，工具可用后会自动继续。',
  },
  {
    test: /未登录\s*115|找不到 cookie|cookie 失效|登录已失效/i,
    reason: '115 登录已失效',
    action: '点右上角重新登录 115，然后重试上传。',
  },
  {
    test: /Access denied by bucket policy/i,
    reason: '115 拒绝了本机的上传请求（存储桶策略）',
    action: '不是文件本身的问题；程序会暂停上传并自动复查，恢复后点“重试失败”或等待自动继续。',
  },
  {
    test: /VIDEO_HUB_CID/,
    reason: '没有配置 115 视频总目录',
    action: '在 .env 设置 VIDEO_HUB_CID 后重启服务。',
  },
  {
    test: /No space left|磁盘空间不足|可用空间/i,
    reason: '磁盘空间不足',
    action: '清理下载目录或配置备用盘（DOWNLOAD_FALLBACK_DIRS）后重试。',
  },
  {
    test: /HTTP Error 410|HTTP 410|HLSSourceUnavailable|SourceUnavailable/i,
    reason: '分到了不可用的 CDN 节点（410）',
    action: '已自动换节点复查；也可以直接重试。',
  },
  {
    test: /HTTP Error 429|HTTP 429|Too Many Requests/i,
    reason: 'PH 视频 CDN 限流（429）',
    action: '不是视频本身的问题；程序会暂停新下载、冷却后自动继续。之前因此失败的可以用“重试可重试项”批量重试。',
  },
  {
    test: /UNEXPECTED_EOF|EOF occurred|ConnectError|ConnectTimeout|connection (reset|refused|aborted)|timed out|timeout|WinError 100(54|60|61)|proxy|tunnel|remote end closed|无字节进展|解析超时/i,
    reason: '网络或代理链路中断',
    action: '多半是代理节点短暂断流；网络恢复后用“重试可重试项”批量重试。',
  },
  {
    test: /时间戳倒退|缺少音频|没有音频包|时间轴.*相差|分片重复|乱序/,
    reason: '下载内容不完整或顺序错乱',
    action: '重试会丢弃这次的临时文件，从零单连接重新下载。',
  },
  {
    test: /外置音轨/,
    reason: '该视频使用当前下载方式不支持的独立音轨',
    action: '重试会尝试其他画质。',
  },
  {
    test: /database is locked|database table is locked/i,
    reason: '数据库短暂繁忙',
    action: '直接重试即可。',
  },
  {
    test: /上传失败|p115client|上传初始化/,
    reason: '上传到 115 失败',
    action: '确认 115 已登录、网络正常后重试。',
  },
  {
    test: /同一输出文件正由任务/,
    reason: '输出文件被另一个任务占用',
    action: '用“重试可重试项”会先校验已有成品，无需重复下载。',
  },
]

export function failureReason(task) {
  const error = String(task?.error || '')
  for (const rule of RULES) {
    const matched = rule.when ? rule.when(task || {}) : rule.test.test(error)
    if (matched) return { reason: rule.reason, action: rule.action }
  }
  return {
    reason: '未归类的错误',
    action: error.trim()
      ? '查看下方原始信息后，再决定是否重试。'
      : '没有记录到具体错误，可以直接重试一次。',
  }
}

/** 弹窗正文：建议在前，原始信息在后，便于复制排查。 */
export function failureDetail(task) {
  const { action } = failureReason(task)
  const raw = String(task?.error || '').trim()
  return raw ? `${action}\n\n原始信息：\n${raw}` : action
}
