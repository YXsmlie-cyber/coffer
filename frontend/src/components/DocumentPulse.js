import { onMounted, onUnmounted, ref, watch } from 'vue'
import { useTaskPulse } from '../composables/useTaskPulse'
import { BASE_TITLE, faviconHref, pulseState, pulseTitle } from '../utils/documentPulse'

// 无界面组件：把全局任务概况写进标签页标题与图标。只挂在已登录的应用外壳里，
// 远程登录完成前不读取任务。
const CLEARED_HOLD_MS = 4000

function setFavicon(state) {
  let link = document.querySelector('link[rel="icon"]')
  if (!link) {
    link = document.createElement('link')
    link.rel = 'icon'
    link.type = 'image/svg+xml'
    document.head.appendChild(link)
  }
  const href = faviconHref(state)
  if (link.getAttribute('href') !== href) link.setAttribute('href', href)
}

export default {
  name: 'DocumentPulse',
  setup() {
    const { summary, loaded } = useTaskPulse()
    // 后台标签里队列跑完时显示“已清空”，回到前台看一眼后自动恢复。
    const cleared = ref(false)
    let wasBusy = false
    let clearTimer = null

    watch(summary, value => {
      const busy = Boolean(value.downloading || value.uploading || value.queued)
      if (loaded.value && wasBusy && !busy && document.hidden) cleared.value = true
      if (busy) cleared.value = false
      wasBusy = busy
    })

    watch([summary, cleared], ([value, isCleared]) => {
      const title = pulseTitle(value, isCleared)
      if (document.title !== title) document.title = title
      setFavicon(pulseState(value, isCleared))
    }, { immediate: true })

    function onVisibility() {
      if (document.hidden || !cleared.value) return
      window.clearTimeout(clearTimer)
      clearTimer = window.setTimeout(() => { cleared.value = false }, CLEARED_HOLD_MS)
    }

    onMounted(() => document.addEventListener('visibilitychange', onVisibility))
    onUnmounted(() => {
      document.removeEventListener('visibilitychange', onVisibility)
      window.clearTimeout(clearTimer)
      document.title = BASE_TITLE
      setFavicon('idle')
    })
    return () => null
  },
}
