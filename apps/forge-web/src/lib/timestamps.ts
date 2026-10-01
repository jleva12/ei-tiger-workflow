/**
 * Times from the APIs. They store and send UTC, as ISO 8601 with its zone
 * ("2026-09-27T16:09:02Z"); the pages show them in the browser's own zone.
 *
 * `new Date` reads a date and time without a zone as the browser's local
 * time, which for a UTC value is hours off, and a bare date as UTC midnight,
 * which west of UTC is the day before. Parse API values here instead.
 */

const DAY = /^(\d{4})-(\d{2})-(\d{2})$/
const WITHOUT_ZONE = /^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?$/

/**
 * When an API time happened: one without a zone is UTC, and a bare date
 * ("2026-09-27") is that calendar day here, from local midnight. Numbers are
 * epoch milliseconds. An unreadable value gives an invalid Date.
 */
export function parseTimestamp(value: string | number | Date): Date {
  if (value instanceof Date) return value
  if (typeof value === "number") return new Date(value)
  const text = value.trim()
  const day = DAY.exec(text)
  if (day) return new Date(Number(day[1]), Number(day[2]) - 1, Number(day[3]))
  if (WITHOUT_ZONE.test(text)) return new Date(`${text.replace(" ", "T")}Z`)
  return new Date(text)
}

/** Whether `value` reads as a time, per `parseTimestamp`. */
export const isTimestamp = (value: string | number | Date) =>
  !Number.isNaN(parseTimestamp(value).getTime())

/** The local calendar day of `date`, "2026-09-27", as date inputs take it. */
export function localDay(date: Date) {
  const pad = (part: number) => String(part).padStart(2, "0")
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`
}
