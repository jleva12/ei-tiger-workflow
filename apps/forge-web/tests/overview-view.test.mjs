import assert from "node:assert/strict"
import { fileURLToPath } from "node:url"
import { test } from "node:test"
import { createServer } from "vite"

const server = await createServer({
  configFile: false,
  root: fileURLToPath(new URL("..", import.meta.url)),
  resolve: { alias: { "@": fileURLToPath(new URL("../src", import.meta.url)) } },
  server: { middlewareMode: true, watch: null },
  optimizeDeps: { noDiscovery: true, include: [] },
})
const { overviewViewOf, spanOf, withView } = await server.ssrLoadModule(
  "/src/features/overview/lib/overview-view.ts"
)
const { daysLabel, periodWords } = await server.ssrLoadModule(
  "/src/features/overview/lib/overview.ts"
)
await server.close()

test("the address keeps what the overview can show, and nothing else", () => {
  assert.deepEqual(overviewViewOf({}), {})
  assert.deepEqual(overviewViewOf({ period: "30d", only: "agent", focus: "agent:a1" }), {
    period: "30d",
    only: "agent",
    focus: "agent:a1",
  })
  // The default period, unknown ones and odd values are left out.
  assert.deepEqual(
    overviewViewOf({ period: "7d", only: "people", focus: "bogus", view: "agents" }),
    {}
  )
  assert.deepEqual(overviewViewOf({ focus: "agent:has space" }), {})
  // Days picked win over a period; days the wrong way round don't count.
  assert.deepEqual(overviewViewOf({ period: "30d", from: "2026-09-01", to: "2026-09-10" }), {
    from: "2026-09-01",
    to: "2026-09-10",
  })
  assert.deepEqual(overviewViewOf({ period: "30d", from: "2026-09-10", to: "2026-09-01" }), {
    period: "30d",
  })
  assert.deepEqual(overviewViewOf({ from: "Sept 1", to: "2026-09-10" }), {})
})

test("the overview reads the days picked, or its period", () => {
  assert.equal(spanOf({}), "7d")
  assert.equal(spanOf({ period: "90d" }), "90d")
  assert.deepEqual(spanOf({ period: "90d", from: "2026-09-01", to: "2026-09-02" }), {
    from: "2026-09-01",
    to: "2026-09-02",
  })
})

test("a change keeps the rest of the address and one period at a time", () => {
  const search = { view: "overview", period: "30d", focus: "agent:a1" }
  assert.deepEqual(withView(search, { from: "2026-09-01", to: "2026-09-10" }), {
    view: "overview",
    period: undefined,
    from: "2026-09-01",
    to: "2026-09-10",
    focus: "agent:a1",
  })
  const picked = { from: "2026-09-01", to: "2026-09-10", only: "agent" }
  assert.deepEqual(withView(picked, { period: "90d" }), {
    period: "90d",
    from: undefined,
    to: undefined,
    only: "agent",
  })
  // The default period is left out.
  assert.equal(withView(picked, { period: "7d" }).period, undefined)
  assert.deepEqual(withView(search, { focus: undefined }), {
    view: "overview",
    period: "30d",
    focus: undefined,
  })
})

test("a period is worded as a sentence, a label and a length", () => {
  assert.deepEqual(periodWords({ period: "30d", first_day: "2026-09-18", last_day: "2026-10-17" }), {
    within: "in the last 30 days",
    opening: "In the last 30 days",
    label: "Last 30 days",
    length: "30 days",
  })
  const year = new Date().getFullYear()
  const picked = periodWords({
    period: "custom",
    first_day: `${year}-09-01`,
    last_day: `${year}-09-10`,
  })
  assert.deepEqual(picked, {
    within: "between Sep 1 and Sep 10",
    opening: "Between Sep 1 and Sep 10,",
    label: "Sep 1 – Sep 10",
    length: "10 days",
  })
  const one = periodWords({ period: "custom", first_day: `${year}-09-01`, last_day: `${year}-09-01` })
  assert.equal(one.within, "on Sep 1")
  assert.equal(one.length, "1 day")
  // Years show when the days aren't this year's, or aren't the same one.
  assert.equal(daysLabel("2025-12-20", "2026-01-05"), "Dec 20, 2025 – Jan 5, 2026")
})
