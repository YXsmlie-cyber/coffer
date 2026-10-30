import test from 'node:test'
import assert from 'node:assert/strict'
import { componentHarness } from './component-harness.mjs'

function carouselHarness({ width = 320, count = 4, pointerType = 'mouse' } = {}) {
  let now = 1000
  let captured = null
  const stage = {
    clientWidth: width,
    setPointerCapture(id) { captured = id },
    releasePointerCapture(id) { if (captured === id) captured = null },
  }
  const { state } = componentHarness('components/CoverCarousel.vue', {
    defineProps: () => ({ videos: Array.from({ length: count }, (_, i) => ({ viewkey: `cover-${i}` })) }),
    watch() {}, onBeforeUnmount() {}, onDeactivated() {},
    resolveApiAssetUrl: value => value,
    Date: { now: () => now },
    isTopModal: () => true,
  }, ['pointerDown', 'pointerMove', 'pointerEnd', 'openLightbox', 'closeLightbox', 'index', 'lightboxOpen', 'dragging'])
  function event(x = 100, y = 100, overrides = {}) {
    return {
      clientX: x, clientY: y, pointerId: 1, pointerType, button: 0,
      currentTarget: stage, prevented: false, stopped: false,
      preventDefault() { this.prevented = true },
      stopPropagation() { this.stopped = true },
      ...overrides,
    }
  }
  return {
    state, event,
    get captured() { return captured },
    advance: milliseconds => { now += milliseconds },
    // 指针捕获在 pointerup 时会把合成 click 定向到捕获元素；
    // 轮播 stage 没有 click 处理器，只有内部封面按钮可以打开大图。
    tap() {
      state.pointerDown(event())
      const clickReachesButton = captured === null
      state.pointerEnd(event())
      if (clickReachesButton) state.openLightbox(event())
    },
    drag(dx, dy = 0, cancelled = false) {
      state.pointerDown(event())
      const move = event(100 + dx, 100 + dy)
      state.pointerMove(move)
      state.pointerEnd(move, cancelled)
      return move
    },
  }
}

test('多封面鼠标和触摸轻点保留内部按钮 click，单封面也能打开', () => {
  for (const options of [{}, { pointerType: 'touch' }, { count: 1 }]) {
    const page = carouselHarness(options)
    page.tap()
    assert.equal(page.state.lightboxOpen.value, true)
    assert.equal(page.state.index.value, 0)
    assert.equal(page.state.dragging.value, false)
  }
})

test('仅超过原 8px 横向意图边界才捕获指针，纵向滚动不捕获', () => {
  const page = carouselHarness()
  page.state.pointerDown(page.event())
  page.state.pointerMove(page.event(108, 100))
  assert.equal(page.captured, null)
  page.state.pointerMove(page.event(109, 110))
  assert.equal(page.captured, null)
  const horizontal = page.event(109, 100)
  page.state.pointerMove(horizontal)
  assert.equal(page.captured, 1)
  assert.equal(horizontal.prevented, true)
  page.state.pointerEnd(horizontal)
  assert.equal(page.captured, null)
  assert.equal(page.state.index.value, 0)
})

test('保留按宽度计算的 36–64px 翻页边界和左右循环顺序', () => {
  for (const [width, threshold] of [[200, 36], [320, 44.8], [1000, 64]]) {
    const page = carouselHarness({ width })
    page.drag(-threshold + 0.01)
    assert.equal(page.state.index.value, 0)
    page.drag(-threshold - 0.01)
    assert.equal(page.state.index.value, 1)
    page.drag(threshold + 0.01)
    assert.equal(page.state.index.value, 0)
    page.drag(threshold + 0.01)
    assert.equal(page.state.index.value, 3)
  }
})

test('拖动不打开大图，原 400ms click 抑制边界保持', () => {
  const page = carouselHarness()
  page.drag(-60)
  const click = page.event()
  page.state.openLightbox(click)
  assert.equal(page.state.index.value, 1)
  assert.equal(page.state.lightboxOpen.value, false)
  assert.equal(click.prevented, true)
  assert.equal(click.stopped, true)
  page.advance(399)
  page.state.openLightbox(page.event())
  assert.equal(page.state.lightboxOpen.value, false)
  page.advance(1)
  page.tap()
  assert.equal(page.state.lightboxOpen.value, true)
})

test('取消手势、纵向手势、非当前指针及非主鼠标键不翻页', () => {
  const cancelled = carouselHarness()
  cancelled.drag(-60, 0, true)
  assert.equal(cancelled.state.index.value, 0)
  assert.equal(cancelled.state.dragging.value, false)
  const vertical = carouselHarness()
  vertical.drag(-60, 70)
  assert.equal(vertical.state.index.value, 0)
  const foreign = carouselHarness()
  foreign.state.pointerDown(foreign.event())
  foreign.state.pointerMove(foreign.event(0, 100, { pointerId: 2 }))
  foreign.state.pointerEnd(foreign.event(0, 100, { pointerId: 2 }))
  assert.equal(foreign.state.index.value, 0)
  assert.equal(foreign.captured, null)
  foreign.state.pointerEnd(foreign.event())
  assert.equal(foreign.state.dragging.value, false)
  const secondary = carouselHarness()
  secondary.state.pointerDown(secondary.event(100, 100, { button: 2 }))
  secondary.state.pointerEnd(secondary.event(0, 100, { button: 2 }))
  assert.equal(secondary.state.index.value, 0)
  assert.equal(secondary.state.dragging.value, false)
})
