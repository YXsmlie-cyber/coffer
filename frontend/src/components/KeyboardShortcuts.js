import { onMounted, onUnmounted } from 'vue'
import { useRouter } from 'vue-router'
import { alert } from '../composables/useDialog'
import { SHORTCUT_HELP, shortcutAction } from '../utils/shortcuts.js'

// 无界面组件：/ 聚焦搜索，g + 字母切换页面，? 显示说明。只挂在已登录外壳里；
// 有弹窗打开时一律不处理，避免和弹窗自身的键盘行为冲突。
function modalOpen() {
  return [...document.querySelectorAll('[aria-modal="true"]')]
    .some(dialog => dialog.getClientRects().length > 0)
}

function focusSearch() {
  const inputs = [...document.querySelectorAll('#main-content input[type="search"]')]
  const visible = inputs.find(input => input.getClientRects().length > 0 && !input.disabled)
  if (!visible) return false
  visible.focus()
  visible.select?.()
  return true
}

export default {
  name: 'KeyboardShortcuts',
  setup() {
    const router = useRouter()
    let pending = null

    function onKeydown(event) {
      if (modalOpen()) {
        pending = null
        return
      }
      const result = shortcutAction(event, pending)
      pending = result.pending
      const action = result.action
      if (!action) return
      if (action === 'focus-search') {
        if (focusSearch()) event.preventDefault()
      } else if (action === 'help') {
        event.preventDefault()
        void alert({
          title: '键盘快捷键',
          message: SHORTCUT_HELP.map(([keys, text]) => `${keys}　${text}`).join('\n'),
        })
      } else if (action.go) {
        event.preventDefault()
        if (router.currentRoute.value.path !== action.go) void router.push(action.go)
      }
    }

    onMounted(() => window.addEventListener('keydown', onKeydown))
    onUnmounted(() => window.removeEventListener('keydown', onKeydown))
    return () => null
  },
}
