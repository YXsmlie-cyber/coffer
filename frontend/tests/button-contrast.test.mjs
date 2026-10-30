import { readFileSync } from 'node:fs'
import test from 'node:test'
import assert from 'node:assert/strict'

const tokens = readFileSync(new URL('../src/styles/tokens.css', import.meta.url), 'utf8')
const darkStart = tokens.indexOf('@media (prefers-color-scheme: dark)')
assert.ok(darkStart > 0, '缺少深色主题覆盖块')
const lightBlock = tokens.slice(0, darkStart)
const darkBlock = tokens.slice(darkStart, tokens.indexOf('\n}\n', darkStart))

function readToken(block, name) {
  return block.match(new RegExp(`--${name}:\\s*(#[a-f0-9]{6})`, 'i'))?.[1]
}
// 深色块只覆盖颜色；未覆盖的令牌沿用浅色值。
const themes = {
  浅色: name => readToken(lightBlock, name),
  深色: name => readToken(darkBlock, name) || readToken(lightBlock, name),
}
function luminance(hex) {
  const channels = hex.slice(1).match(/../g).map(value => Number.parseInt(value, 16) / 255)
    .map(value => value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4)
  return channels[0] * 0.2126 + channels[1] * 0.7152 + channels[2] * 0.0722
}
function contrast(left, right) {
  const values = [luminance(left), luminance(right)].sort((a, b) => b - a)
  return (values[0] + 0.05) / (values[1] + 0.05)
}
function assertPair(theme, token, foreground, background) {
  const fg = token(foreground)
  const bg = token(background)
  assert.ok(fg && bg, `${theme}缺少颜色令牌 ${foreground} 或 ${background}`)
  const ratio = contrast(fg, bg)
  assert.ok(ratio >= 4.5, `${theme} ${foreground} / ${background} 对比度 ${ratio.toFixed(3)} : 1 低于 4.5`)
}

test('主动作与危险确认按钮在默认/悬停状态保持普通文字对比度', () => {
  for (const [theme, token] of Object.entries(themes)) {
    for (const background of ['button-bg', 'button-hover', 'danger-bg', 'danger-hover']) {
      assertPair(theme, token, 'button-text', background)
    }
  }
})

test('辅助文字在三层常用卡片表面保持可读', () => {
  for (const [theme, token] of Object.entries(themes)) {
    for (const background of ['bg', 'surface', 'surface-2', 'surface-3']) {
      assertPair(theme, token, 'muted-2', background)
    }
  }
})

test('状态与链接文字在卡片和页面底色上可读', () => {
  for (const [theme, token] of Object.entries(themes)) {
    for (const foreground of ['accent-2', 'amber', 'ok', 'warn', 'err', 'violet']) {
      for (const background of ['bg', 'surface']) assertPair(theme, token, foreground, background)
    }
  }
})
