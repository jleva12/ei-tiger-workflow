import { parseTimestamp } from "@/lib/timestamps"

/*
 * How the overview writes its figures and labels its charts' dates, as
 * forge-aidlc-parent's delivery overviews do.
 */

export const MINUTE = 60_000
export const HOUR = 60 * MINUTE
export const DAY = 24 * HOUR

/** Dollars: cents under $10, whole dollars above; `<$0.01` for a trace. */
export function usd(value: number) {
  if (value > 0 && value < 0.01) return "<$0.01"
  return value.toLocaleString("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: value < 10 ? 2 : 0,
    maximumFractionDigits: value < 10 ? 2 : 0,
  })
}

export const percent = (value: number) => `${Math.round(value * 100)}%`

const compactFormat = new Intl.NumberFormat("en-US", {
  notation: "compact",
  maximumFractionDigits: 1,
})

/** A count, compactly: `940`, `536.7K`, `1.2M`. */
export const compact = (value: number) =>
  compactFormat.format(Math.round(value))

/** A rate, with a decimal while it's small: `0.4`, `3.2`, `18`, `1.2K`. */
export const rate = (value: number) =>
  value > 0 && value < 10
    ? value.toFixed(1).replace(/\.0$/, "")
    : compact(value)

/** A whole count, in full: `4,102`. */
export const count = (value: number) => Math.round(value).toLocaleString()

export const plural = (n: number, noun: string, many = `${noun}s`) =>
  `${n.toLocaleString()} ${n === 1 ? noun : many}`

/** How long something took: `850 ms`, `12 s`, `3.4 min`, `1.2 h`. */
export function duration(ms: number) {
  if (ms < 1000) return `${Math.round(ms)} ms`
  if (ms < MINUTE) return `${(ms / 1000).toFixed(ms < 10_000 ? 1 : 0)} s`
  if (ms < HOUR) return `${(ms / MINUTE).toFixed(1)} min`
  return `${(ms / HOUR).toFixed(1)} h`
}

/** How long something has waited: `40 min`, `7 h`, `2 d 4 h`. */
export function age(ms: number) {
  if (ms < HOUR) return `${Math.max(1, Math.round(ms / MINUTE))} min`
  if (ms < DAY) return `${Math.floor(ms / HOUR)} h`
  const d = Math.floor(ms / DAY)
  const h = Math.floor((ms % DAY) / HOUR)
  return h ? `${d} d ${h} h` : `${d} d`
}

/** The same, in words for a sentence: `7 hours`, `2 days`. */
export function ageInWords(ms: number) {
  if (ms < HOUR) return "under an hour"
  if (ms < DAY) return plural(Math.floor(ms / HOUR), "hour")
  return plural(Math.floor(ms / DAY), "day")
}

/** When something last happened: `2 days ago`, or never. */
export function since(iso: string | null, now: number) {
  if (!iso) return "Never"
  return `${ageInWords(Math.max(0, now - parseTimestamp(iso).getTime()))} ago`
}

const dateFormat = new Intl.DateTimeFormat("en-US", {
  month: "short",
  day: "numeric",
})
const weekdayFormat = new Intl.DateTimeFormat("en-US", {
  weekday: "short",
  month: "short",
  day: "numeric",
})

export type Unit = "day" | "week"

/** A bucket as a tooltip and a table name it: `Mon, Oct 5` or `Week of Oct 5`. */
export function bucketLabel(start: number | undefined, unit: Unit) {
  if (start === undefined) return ""
  return unit === "week"
    ? `Week of ${dateFormat.format(start)}`
    : weekdayFormat.format(start)
}

/** An axis tick: `Sep 22`. */
export const tick = (value: number) => dateFormat.format(value)

/**
 * The dates every chart labels, so the panels line up: every other day for
 * 7 days (counting back from today), each Monday for 30, every other week's
 * start for 90.
 */
export function sharedTicks(starts: number[], unit: Unit) {
  if (unit === "week") {
    return starts.filter((_, index) => (starts.length - 1 - index) % 2 === 0)
  }
  if (starts.length <= 7) {
    return starts.filter((_, index) => (starts.length - 1 - index) % 2 === 0)
  }
  return starts.filter((start) => new Date(start).getDay() === 1)
}
