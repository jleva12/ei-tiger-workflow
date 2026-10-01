import assert from "node:assert/strict"
import { fileURLToPath } from "node:url"
import { test } from "node:test"
import { createServer } from "vite"

// West of UTC, where a UTC time read as local is hours off and a bare date
// read as UTC is the day before. Set before any Date is made.
process.env.TZ = "America/Los_Angeles"

const server = await createServer({
  configFile: false,
  root: fileURLToPath(new URL("..", import.meta.url)),
  resolve: { alias: { "@": fileURLToPath(new URL("../src", import.meta.url)) } },
  server: { middlewareMode: true, watch: null },
  optimizeDeps: { noDiscovery: true, include: [] },
})
const { parseTimestamp, isTimestamp, localDay } = await server.ssrLoadModule("/src/lib/timestamps.ts")
const { formatValue } = await server.ssrLoadModule("/src/components/forge/data-table/utils.ts")
await server.close()

test("an API time with its zone is that instant", () => {
  assert.equal(parseTimestamp("2026-09-27T16:09:02.233208Z").toISOString(), "2026-09-27T16:09:02.233Z")
  assert.equal(parseTimestamp("2026-09-27T18:09:02+02:00").toISOString(), "2026-09-27T16:09:02.000Z")
})

test("an API time without a zone is UTC, not the browser's own time", () => {
  assert.equal(parseTimestamp("2026-09-27T05:55:15.930996").toISOString(), "2026-09-27T05:55:15.930Z")
  assert.equal(parseTimestamp("2026-09-27 05:55:15").toISOString(), "2026-09-27T05:55:15.000Z")
  assert.equal(parseTimestamp("2026-09-27T05:55").toISOString(), "2026-09-27T05:55:00.000Z")
})

test("a bare date is that day here, not the day before", () => {
  const day = parseTimestamp("2026-09-27")
  assert.deepEqual([day.getFullYear(), day.getMonth(), day.getDate(), day.getHours()], [2026, 8, 27, 0])
  assert.equal(localDay(day), "2026-09-27")
})

test("the local day of a UTC time is the day it is here", () => {
  // 02:00 UTC on the 28th is still the evening of the 27th in Los Angeles.
  assert.equal(localDay(parseTimestamp("2026-09-28T02:00:00Z")), "2026-09-27")
})

test("epoch milliseconds, Dates and junk", () => {
  assert.equal(parseTimestamp(0).toISOString(), "1970-01-01T00:00:00.000Z")
  const now = new Date()
  assert.equal(parseTimestamp(now), now)
  assert.equal(isTimestamp("not a date"), false)
  assert.equal(isTimestamp(""), false)
})

test("tables show times in the browser's zone", () => {
  const formatOptions = { dateStyle: "medium", timeStyle: "short" }
  const local = new Intl.DateTimeFormat(undefined, formatOptions)
  // 16:09 UTC is 09:09 in Los Angeles.
  const expected = local.format(new Date(2026, 8, 27, 9, 9, 2))
  const options = { format: "datetime", formatOptions }
  assert.equal(formatValue("2026-09-27T16:09:02Z", options), expected)
  assert.equal(formatValue("2026-09-27T16:09:02", options), expected)
  const day = new Intl.DateTimeFormat(undefined, { dateStyle: "medium" })
  assert.equal(formatValue("2026-09-27", { format: "date" }), day.format(new Date(2026, 8, 27)))
})
