import { onMounted, onUnmounted } from 'vue'
import { isTopModal } from '../utils/modalStack'

/**
 * 模态可访问性辅助：监听 Esc 键关闭。
 * @param {Function} onClose 关闭回调
 * @param {import('vue').Ref<HTMLElement|null>} [rootRef] 当前弹窗根节点
 *
 * 用法：在模态组件 setup 中调用 useEscClose(() => emit('close'))。
 * 仅最上层弹窗响应，避免确认框叠在详情框上时一次 Esc 把两层都关闭。
 */
export function useEscClose(onClose, rootRef = null) {
  function onKey(e) {
    if (e.key !== 'Escape') return
    if (rootRef?.value && !isTopModal(rootRef.value)) return
    onClose()
  }
  onMounted(() => window.addEventListener('keydown', onKey))
  onUnmounted(() => window.removeEventListener('keydown', onKey))
}
