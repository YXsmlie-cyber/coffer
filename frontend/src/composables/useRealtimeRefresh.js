import { onMounted, onUnmounted, onActivated, onDeactivated } from 'vue'
import { subscribeWSRefresh } from '../api/websocket'

/** 当前可见页面在恢复连接/回到前台时合并补读；不增加周期轮询。 */
export function useRealtimeRefresh(refresh) {
  let unsubscribe = null
  let timer = null
  let active = false
  const activate = () => {
    active = true
    unsubscribe ||= subscribeWSRefresh(() => {
      if (timer !== null) return
      timer = window.setTimeout(() => {
        timer = null
        Promise.resolve().then(() => {
          if (active && !document.hidden) return refresh()
        }).catch(() => {})
      }, 300)
    })
  }
  const deactivate = () => {
    active = false
    unsubscribe?.()
    unsubscribe = null
    if (timer !== null) window.clearTimeout(timer)
    timer = null
  }
  onMounted(activate)
  onActivated(activate)
  onDeactivated(deactivate)
  onUnmounted(deactivate)
}
