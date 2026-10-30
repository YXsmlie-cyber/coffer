import { readFileSync } from 'node:fs'
import vm from 'node:vm'

// 运行真实组件的脚本；网络、Vue 生命周期和计时器由测试控制。
export function componentHarness(filename, bindings = {}, exposed = []) {
  const text = readFileSync(new URL(`../src/${filename}`, import.meta.url), 'utf8')
  const script = text.includes('<script setup>')
    ? text.split('<script setup>')[1].split('</script>')[0]
    : text
  const mounted = []
  const unmounted = []
  const emitted = []
  const timers = new Map()
  let timerId = 0
  const context = {
    ref: value => ({ value }),
    reactive: value => value,
    computed: getter => ({ get value() { return getter() } }),
    defineEmits: () => (...args) => emitted.push(args),
    onMounted: callback => mounted.push(callback),
    onUnmounted: callback => unmounted.push(callback),
    useEscClose() {},
    useModalFocus() {},
    apiErrorText: error => error.message,
    setTimeout(callback, delay) {
      const id = ++timerId
      timers.set(id, { callback, delay })
      return id
    },
    clearTimeout: id => timers.delete(id),
    ...bindings,
  }
  vm.createContext(context)
  const executable = script.replace(/^import[\s\S]*?from ['"][^'"]+['"]\r?\n/gm, '')
  vm.runInContext(`${executable}\nglobalThis.exposed = {${exposed.join(',')}}`, context)
  return {
    state: context.exposed,
    emitted,
    timers,
    mount: () => mounted.forEach(callback => callback()),
    unmount: () => unmounted.forEach(callback => callback()),
    runTimer() {
      const entry = timers.entries().next().value
      if (!entry) throw new Error('没有待执行的计时器')
      const [id, timer] = entry
      timers.delete(id)
      return timer.callback()
    },
  }
}

export function deferred() {
  let resolve
  let reject
  const promise = new Promise((accept, fail) => { resolve = accept; reject = fail })
  return { promise, resolve, reject }
}

export const flush = () => new Promise(resolve => setImmediate(resolve))
