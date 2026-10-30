import { resolveApiAssetUrl } from './assetUrl.js'

/** 模特卡与详情抽屉共用的展示规则；只读模型字段，不发请求。 */
export function modelProgress(model) {
  if (!model?.total_videos) return 0
  return Math.round(model.cloud_videos / model.total_videos * 100)
}

export function downloadableCount(model) {
  return model?.downloadable_videos ?? model?.missing_videos ?? 0
}

export function avatarStatusText(status) {
  return {
    source_no_avatar: '源站无头像',
    source_unavailable: '主页已失效',
    source_broken: '源头像失效',
    fetch_error: '头像审计失败',
    ready: '头像文件异常',
    pending: '头像未入库',
  }[status] || '头像未入库'
}

/** 首张作品封面的候选地址：本机清晰封面优先，失败后回退源站缩略图。 */
export function coverSources(model) {
  const first = model?.preview_videos?.[0]
  if (!first) return []
  return [resolveApiAssetUrl(first.preview_image_url), first.thumbnail]
    .filter((value, index, list) => value && list.indexOf(value) === index)
}

/** 卡片角标：同步中 > 未同步 > 缺失 > 待上传 > 已齐。 */
export function modelState(model, syncing = false) {
  if (syncing) return { tone: 'syncing', text: '同步中' }
  if (!model?.synced_at) return { tone: 'idle', text: '未同步' }
  const missing = Number(model.missing_videos || 0)
  const pending = Number(model.pending_upload_videos || 0)
  if (missing > 0) return { tone: 'missing', text: `缺 ${missing}` }
  if (pending > 0) return { tone: 'pending', text: `待上传 ${pending}` }
  return { tone: 'done', text: '已齐' }
}

export function rankTone(rank) {
  return { 1: 'top1', 2: 'top2', 3: 'top3' }[rank] || ''
}
