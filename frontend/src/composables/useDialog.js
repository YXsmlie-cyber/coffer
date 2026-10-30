import { reactive } from 'vue'

/**
 * 全局确认/提示对话框服务（命令式）。
 *
 * 替代原生 confirm()/alert()，统一 OLED 风格。用法：
 *   import { confirm, alert } from '../composables/useDialog'
 *   if (await confirm({ title: '删除？', message: '...', danger: true })) { ... }
 *   await alert({ title: '失败', message: '...' })
 *
 * 单例状态由挂在 App 根部的 <ConfirmDialog> 渲染。一次只显示一个对话框
 * （本项目不需要叠加）；新调用会覆盖未决的上一个并将其 resolve(false)。
 */
export const dialogState = reactive({
  open: false,
  title: '',
  message: '',
  confirmText: '确定',
  cancelText: '取消',
  danger: false,
  alertOnly: false,
  _resolve: null,
})

function _show(opts, alertOnly) {
  // 覆盖未决对话框：旧的当作取消处理
  if (dialogState._resolve) dialogState._resolve(false)
  dialogState.open = true
  dialogState.alertOnly = alertOnly
  dialogState.title = opts.title || (alertOnly ? '提示' : '确认')
  dialogState.message = opts.message || ''
  dialogState.confirmText = opts.confirmText || (alertOnly ? '知道了' : '确定')
  dialogState.cancelText = opts.cancelText || '取消'
  dialogState.danger = !!opts.danger
  return new Promise((resolve) => { dialogState._resolve = resolve })
}

/** 确认对话框，返回 Promise<boolean>。 */
export function confirm(opts = {}) {
  return _show(opts, false)
}

/** 提示对话框（仅一个确认按钮），返回 Promise<void>。 */
export function alert(opts = {}) {
  return _show(opts, true).then(() => undefined)
}

/** 由 ConfirmDialog 组件调用，关闭并 resolve。 */
export function _settle(result) {
  const r = dialogState._resolve
  dialogState.open = false
  dialogState._resolve = null
  if (r) r(result)
}
