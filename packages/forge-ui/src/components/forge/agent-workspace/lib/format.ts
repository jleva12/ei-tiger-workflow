const compact = new Intl.NumberFormat("en", {
  notation: "compact",
  maximumFractionDigits: 1,
})
const whole = new Intl.NumberFormat("en")

/** 845, 12.4k, 1.2M. */
export const tokens = (count: number) => compact.format(count)

/** 12,345. */
export const exactTokens = (count: number) => whole.format(count)

/** 0.8s, 12s, 1m 5s. */
export function duration(seconds: number) {
  if (seconds < 10) return `${seconds.toFixed(1)}s`
  if (seconds < 60) return `${Math.round(seconds)}s`
  return `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`
}

/** $0.0004, $0.12, $3.40; "<$0.0001" for a fraction of that. */
export function usd(amount: number) {
  if (amount === 0) return "$0"
  if (amount < 0.0001) return "<$0.0001"
  if (amount < 0.01) return `$${amount.toPrecision(2)}`
  return `$${amount.toFixed(2)}`
}
