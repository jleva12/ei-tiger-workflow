import { isTimestamp, parseTimestamp } from "@/lib/timestamps"

/*
 * How the console writes dates, durations and sizes: absolute, short,
 * relative ("5 minutes ago"), elapsed ("3m 42s") and in bytes ("12.4 MB").
 */

const dateFormat = new Intl.DateTimeFormat(undefined, { dateStyle: "medium" })
const dateTimeFormat = new Intl.DateTimeFormat(undefined, {
  dateStyle: "medium",
  timeStyle: "short",
})
const shortDateTimeFormat = new Intl.DateTimeFormat(undefined, {
  month: "short",
  day: "numeric",
  hour: "numeric",
  minute: "2-digit",
})

const valid = (iso: string) => isTimestamp(iso)

/** "Sep 8, 2026". */
export const formatDate = (iso: string) =>
  valid(iso) ? dateFormat.format(parseTimestamp(iso)) : iso
/** "Sep 8, 2026, 9:01 AM". */
export const formatDateTime = (iso: string) =>
  valid(iso) ? dateTimeFormat.format(parseTimestamp(iso)) : iso
/** "Sep 8, 9:01 AM". */
export const formatShortDateTime = (iso: string) =>
  valid(iso) ? shortDateTimeFormat.format(parseTimestamp(iso)) : iso

const relativeFormat = new Intl.RelativeTimeFormat(undefined, {
  numeric: "auto",
})

/** "just now", "5 minutes ago", "yesterday"; past a week, "Sep 8, 2026". */
export function formatRelative(iso: string, now = Date.now()) {
  if (!valid(iso)) return iso
  const seconds = Math.round((parseTimestamp(iso).getTime() - now) / 1000)
  const abs = Math.abs(seconds)
  if (abs < 45) return "just now"
  if (abs < 3600)
    return relativeFormat.format(Math.round(seconds / 60), "minute")
  if (abs < 86400)
    return relativeFormat.format(Math.round(seconds / 3600), "hour")
  if (abs < 7 * 86400)
    return relativeFormat.format(Math.round(seconds / 86400), "day")
  return formatDate(iso)
}

/** "3m 42s", "1h 5m", "850 ms". */
export function formatDuration(ms: number) {
  if (!Number.isFinite(ms) || ms < 0) return "—"
  if (ms < 1000) return `${Math.round(ms)} ms`
  const seconds = Math.round(ms / 1000)
  if (seconds < 60) return `${seconds}s`
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes}m ${seconds % 60}s`
  const hours = Math.floor(minutes / 60)
  return `${hours}h ${minutes % 60}m`
}

const BYTE_UNITS = ["B", "KB", "MB", "GB", "TB"]

/** "12.4 MB": decimal units, one decimal under 100. */
export function formatBytes(bytes: number) {
  let value = bytes
  let unit = 0
  while (value >= 1000 && unit < BYTE_UNITS.length - 1) {
    value /= 1000
    unit += 1
  }
  const digits = unit === 0 || value >= 100 ? 0 : 1
  return `${value.toFixed(digits).replace(/\.0$/, "")} ${BYTE_UNITS[unit]}`
}
