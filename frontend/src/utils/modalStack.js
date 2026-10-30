/** 仅最上层可见弹窗响应键盘；确认框叠加时保留底层弹窗。 */
export function isTopModal(element) {
  if (!element) return false
  const dialogs = [...document.querySelectorAll('[aria-modal="true"]')]
    .filter(dialog => dialog.getClientRects().length > 0)
  return dialogs.at(-1) === element
}
