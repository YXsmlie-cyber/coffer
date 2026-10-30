import { gzipSync } from 'node:zlib'
import { readFileSync, readdirSync } from 'node:fs'
import { basename, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const distDir = new URL('../dist/', import.meta.url)
const assetsDir = new URL('./assets/', distDir)
const assetsPath = fileURLToPath(assetsDir)
const html = readFileSync(new URL('./index.html', distDir), 'utf8')
const entryMatch = html.match(/<script[^>]+src="[^"]*\/([^/"?]+\.js)"/i)

if (!entryMatch) {
  throw new Error('无法从 dist/index.html 识别入口 JavaScript')
}

const entryName = basename(entryMatch[1])
const files = readdirSync(assetsPath).filter(file => file.endsWith('.js'))
const sizes = files.map(file => ({
  file,
  gzipBytes: gzipSync(readFileSync(join(assetsPath, file)), { level: 9 }).length,
}))

const entryBudget = 80 * 1024
const routeChunkBudget = 130 * 1024
const failures = []
for (const item of sizes) {
  const budget = item.file === entryName ? entryBudget : routeChunkBudget
  if (item.gzipBytes > budget) failures.push({ ...item, budget })
}

for (const item of sizes.sort((left, right) => right.gzipBytes - left.gzipBytes)) {
  const kind = item.file === entryName ? '入口' : '按需'
  console.log(`${kind}\t${(item.gzipBytes / 1024).toFixed(2)} KiB gzip\t${item.file}`)
}

// CSS 仍按固定级联顺序合并交付；一起约束总量，避免视觉样式持续膨胀。
const cssBytes = readdirSync(assetsPath)
  .filter(file => file.endsWith('.css'))
  .reduce((total, file) => total + gzipSync(
    readFileSync(join(assetsPath, file)), { level: 9 }).length, 0)
const cssBudget = 20 * 1024
if (cssBytes > cssBudget) failures.push({ file: '全部 CSS', gzipBytes: cssBytes, budget: cssBudget })
console.log(`样式\t${(cssBytes / 1024).toFixed(2)} KiB gzip\t全部 CSS（预算 ${cssBudget / 1024} KiB）`)

if (failures.length) {
  const details = failures.map(item => (
    `${item.file}: ${(item.gzipBytes / 1024).toFixed(2)} KiB > ${item.budget / 1024} KiB`
  )).join('\n')
  throw new Error(`前端包体预算超限：\n${details}`)
}

console.log(`包体预算通过：入口 ≤ ${entryBudget / 1024} KiB，单个按需 chunk ≤ ${routeChunkBudget / 1024} KiB，全部 CSS ≤ ${cssBudget / 1024} KiB（gzip）`)
