import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

test('虚拟列表计算与 CSS 固定行高保持同一契约', () => {
  const css = readFileSync(new URL('../src/styles/tasks-dialogs.css', import.meta.url), 'utf8')
  for (const [file, constant, selector, expected] of [
    ['TaskTable.vue', 'ROW_H', '.task-row', 62],
    ['VideoList.vue', 'ROW_HEIGHT', '.vlm-row', 58],
  ]) {
    const script = readFileSync(new URL(`../src/components/${file}`, import.meta.url), 'utf8')
    assert.equal(Number(script.match(new RegExp(`const ${constant} = (\\d+)`))[1]), expected)
    const blocks = [...css.matchAll(/([^{}]+)\{([^{}]+)\}/g)].filter(match => match[1].trim() === selector)
    assert.ok(blocks.some(match => new RegExp(`\\bheight:\\s*${expected}px`).test(match[2])), selector)
  }
})
