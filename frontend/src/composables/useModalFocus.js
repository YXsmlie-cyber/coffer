import { nextTick, onMounted, onUnmounted, watch } from 'vue'
import { isTopModal } from '../utils/modalStack'

const FOCUSABLE = [
  'a[href]', 'button:not([disabled])', 'input:not([disabled])',
  'select:not([disabled])', 'textarea:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(',')

/** 把焦点移入弹窗、限制 Tab 在弹窗内，并在关闭后恢复原焦点。 */
export function useModalFocus(rootRef, openRef = null, { initialFocusRef, restoreFocusRef } = {}) {
  let previous = null
  let active = false
  let generation = 0

  async function activate() {
    const token = ++generation
    active = true
    previous = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null
    await nextTick()
    if (!active || token !== generation || !rootRef.value) return
    document.addEventListener('keydown', trapTab)
    const initialTarget = initialFocusRef?.value || rootRef.value
    initialTarget.focus({ preventScroll: true })
  }

  function deactivate() {
    const wasActive = active
    active = false
    generation += 1
    document.removeEventListener('keydown', trapTab)
    if (wasActive || previous) {
      // 翻页可能替换带 key 的触发按钮，回退到调用方提供的稳定容器。
      const target = previous?.isConnected ? previous : restoreFocusRef?.value
      if (target?.isConnected) target.focus({ preventScroll: true })
    }
    previous = null
  }

  function trapTab(event) {
    if (event.key !== 'Tab' || !active || !isTopModal(rootRef.value)) return
    const items = [...rootRef.value.querySelectorAll(FOCUSABLE)]
      .filter(el => el.getClientRects().length > 0)
    if (!items.length) {
      event.preventDefault()
      rootRef.value.focus()
      return
    }
    const first = items[0]
    const last = items.at(-1)
    const outside = !rootRef.value.contains(document.activeElement)
    if (event.shiftKey && (outside || document.activeElement === first || document.activeElement === rootRef.value)) {
      event.preventDefault()
      last.focus()
    } else if (!event.shiftKey && (outside || document.activeElement === last)) {
      event.preventDefault()
      first.focus()
    }
  }

  let stopWatch = null
  onMounted(() => {
    if (openRef) {
      stopWatch = watch(openRef, value => value ? activate() : deactivate(), { immediate: true })
    } else {
      activate()
    }
  })
  onUnmounted(() => {
    stopWatch?.()
    deactivate()
  })
}
