import {
  computed,
  nextTick,
  onActivated,
  onDeactivated,
  onUnmounted,
  ref,
  watch,
} from 'vue'
import { onBeforeRouteLeave } from 'vue-router'

const PAGE_SIZE = 60

/** 模特过滤、排序、增量渲染与 KeepAlive 位置恢复。 */
export function useModelGrid({
  models,
  search,
  searchResultModelId,
  sortBy,
  progressOf,
  resetSources = [],
}) {
  const visibleCount = ref(PAGE_SIZE)
  const sentinel = ref(null)
  const modelGrid = ref(null)
  let observer = null
  let restoreToken = 0
  let restoring = false
  let userTookControl = false

  const displayModels = computed(() => {
    if (searchResultModelId.value !== null) {
      const result = models.value.find(model => model.id === searchResultModelId.value)
      return result ? [result] : []
    }
    let list = models.value
    const query = search.value.trim().toLocaleLowerCase()
    if (query) {
      list = list.filter(model =>
        model.name.toLocaleLowerCase().includes(query)
        || model.ph_id?.toLocaleLowerCase().includes(query))
    }
    const sorted = [...list]
    if (sortBy.value === 'missing') {
      sorted.sort((a, b) => (b.missing_videos || 0) - (a.missing_videos || 0))
    } else if (sortBy.value === 'progress') {
      sorted.sort((a, b) => progressOf(b) - progressOf(a))
    } else if (sortBy.value === 'name') {
      sorted.sort((a, b) => a.name.localeCompare(b.name))
    }
    return sorted
  })
  const pagedModels = computed(() => displayModels.value.slice(0, visibleCount.value))
  const hasMore = computed(() => visibleCount.value < displayModels.value.length)

  const savedView = {
    hasPosition: false,
    modelId: null,
    viewportTop: 0,
    scrollY: 0,
  }

  function gridElement() {
    return modelGrid.value || document.querySelector('[data-model-grid]')
  }

  function captureViewPosition() {
    savedView.hasPosition = true
    savedView.scrollY = window.scrollY
    savedView.modelId = null
    const grid = gridElement()
    if (!grid) return
    const navBottom = document.querySelector('.nav')?.getBoundingClientRect().bottom || 0
    const cards = Array.from(grid.querySelectorAll('[data-model-id]'))
    const anchor = cards.find(card => card.getBoundingClientRect().bottom > navBottom)
    if (!anchor) return
    savedView.modelId = anchor.dataset.modelId || null
    savedView.viewportTop = anchor.getBoundingClientRect().top
  }

  function afterPaint() {
    return new Promise(resolve => window.requestAnimationFrame(resolve))
  }

  function afterLayoutDelay(delay) {
    return new Promise(resolve => window.setTimeout(resolve, delay))
  }

  function cancelRestoreForUser(event) {
    if (!restoring) return
    if (event.type === 'keydown'
        && !['ArrowUp', 'ArrowDown', 'PageUp', 'PageDown', 'Home', 'End', ' '].includes(event.key)) {
      return
    }
    userTookControl = true
    restoring = false
    restoreToken += 1
  }

  function listenForUserTakeover(enabled) {
    const method = enabled ? 'addEventListener' : 'removeEventListener'
    window[method]('wheel', cancelRestoreForUser, { passive: true })
    window[method]('touchstart', cancelRestoreForUser, { passive: true })
    window[method]('pointerdown', cancelRestoreForUser, { passive: true })
    window[method]('keydown', cancelRestoreForUser)
  }

  async function alignSavedAnchor(anchor, clampScrollTop, token, passes = 4) {
    for (let pass = 0; pass < passes; pass += 1) {
      if (token !== restoreToken || userTookControl) return false
      const delta = anchor.getBoundingClientRect().top - savedView.viewportTop
      if (Math.abs(delta) < 0.5) break
      window.scrollTo({
        left: 0,
        top: clampScrollTop(window.scrollY + delta),
        behavior: 'auto',
      })
      await afterPaint()
    }
    return true
  }

  async function restoreViewPosition() {
    if (!savedView.hasPosition) return
    const token = ++restoreToken
    restoring = true
    userTookControl = false
    listenForUserTakeover(true)
    try {
      await nextTick()
      if (token !== restoreToken) return
      const grid = gridElement()
      await afterPaint()
      const clampScrollTop = top => {
        const maximum = Math.max(0, document.documentElement.scrollHeight - window.innerHeight)
        return Math.min(Math.max(0, top), maximum)
      }
      window.scrollTo({
        left: 0,
        top: clampScrollTop(savedView.scrollY),
        behavior: 'auto',
      })
      await afterPaint()
      await afterPaint()
      if (token !== restoreToken || userTookControl) return

      const anchor = savedView.modelId
        ? grid?.querySelector(`[data-model-id="${savedView.modelId}"]`)
        : null
      if (!anchor) return
      anchor.scrollIntoView({ block: 'start', inline: 'nearest', behavior: 'auto' })
      await afterPaint()
      await afterPaint()
      await alignSavedAnchor(anchor, clampScrollTop, token)
      for (const delay of [80, 120, 180, 260, 360]) {
        await afterLayoutDelay(delay)
        if (!await alignSavedAnchor(anchor, clampScrollTop, token, 2)) break
      }
    } finally {
      if (token === restoreToken) restoring = false
      listenForUserTakeover(false)
    }
  }

  function setupObserver() {
    if (!sentinel.value) return
    if (!window.IntersectionObserver) {
      visibleCount.value = displayModels.value.length
      return
    }
    if (!observer) {
      observer = new IntersectionObserver(entries => {
        if (entries[0].isIntersecting && hasMore.value) {
          visibleCount.value = Math.min(
            visibleCount.value + PAGE_SIZE,
            displayModels.value.length,
          )
        }
      }, { rootMargin: '400px' })
    }
    observer.disconnect()
    observer.observe(sentinel.value)
  }

  watch([search, searchResultModelId, sortBy, ...resetSources], () => {
    visibleCount.value = PAGE_SIZE
  })
  watch(sentinel, element => {
    if (element) setupObserver()
    else if (observer) {
      observer.disconnect()
      observer = null
    }
  })
  onBeforeRouteLeave(captureViewPosition)
  onDeactivated(() => {
    restoreToken += 1
    restoring = false
    listenForUserTakeover(false)
    observer?.disconnect()
  })
  onActivated(async () => {
    await restoreViewPosition()
    setupObserver()
  })
  onUnmounted(() => {
    restoreToken += 1
    listenForUserTakeover(false)
    observer?.disconnect()
  })

  return { displayModels, pagedModels, hasMore, sentinel, modelGrid }
}
