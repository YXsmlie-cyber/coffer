/** 把速度序列换成 SVG polyline 坐标；全为 0 时贴底，单点时画成水平线。 */
export function sparklinePoints(values, width = 120, height = 24, pad = 2) {
  const numbers = (values || []).map(value => (Number.isFinite(Number(value)) ? Math.max(0, Number(value)) : 0))
  if (!numbers.length) return ''
  const series = numbers.length === 1 ? [numbers[0], numbers[0]] : numbers
  const max = Math.max(...series) || 1
  const step = (width - pad * 2) / (series.length - 1)
  return series.map((value, index) => {
    const x = pad + index * step
    const y = height - pad - (value / max) * (height - pad * 2)
    return `${x.toFixed(1)},${y.toFixed(1)}`
  }).join(' ')
}
