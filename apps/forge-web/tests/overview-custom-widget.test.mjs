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
const {
  CUSTOM_SETTINGS,
  DEFAULT_SPEC,
  customData,
  drawable,
  linesOnly,
  specOf,
  titleOf,
  withCustomSpec,
  withCustomWidget,
} = await server.ssrLoadModule("/src/features/overview/lib/custom-widget.ts")
const { withSharedWidget } = await server.ssrLoadModule(
  "/src/features/overview/lib/shared-widgets.ts"
)
await server.close()

const usage = (bucket, subject, model, input, output, cost, calls = 1) => ({
  bucket,
  subject,
  model,
  calls,
  failed: 0,
  input,
  cached: 0,
  output,
  thinking: 0,
  cost,
  unpriced: 0,
})
const activity = (bucket, subject, started, succeeded, failed) => ({
  bucket,
  subject,
  started,
  succeeded,
  failed,
  abandoned: 0,
  open: started - succeeded - failed,
  retried: 0,
  duration_ms: 0,
  timed: 0,
})
const subject = (key, name) => ({
  key,
  kind: key.slice(0, key.indexOf(":")),
  id: key.slice(key.indexOf(":") + 1),
  name,
  current: true,
  last_at: null,
})

const overview = {
  period: "7d",
  unit: "day",
  buckets: [0, 1, 2].map((i) => ({
    start: `2026-10-0${5 + i}T00:00:00+00:00`,
    end: `2026-10-0${6 + i}T00:00:00+00:00`,
  })),
  subjects: [
    subject("workflow:w1", "Intake"),
    subject("agent:a1", "Front desk"),
    subject("agent:a2", "Quiet one"),
  ],
  usage: [
    usage(0, "workflow:w1", "openai/gpt-5", 100, 50, 1),
    usage(1, "workflow:w1", "openai/gpt-5", 200, 100, 2),
    usage(1, "agent:a1", "anthropic/claude", 10, 10, 0.5),
    usage(2, "agent:a2", "openai/gpt-5", 30, 30, 0.25),
    usage(-1, "workflow:w1", "openai/gpt-5", 50, 50, 1),
  ],
  activity: [
    activity(0, "workflow:w1", 4, 3, 1),
    activity(2, "agent:a1", 2, 2, 0),
    activity(-1, "workflow:w1", 2, 1, 1),
  ],
}
const everything = { level: "organization" }
// The ledger's slots: by 12-week tokens; the quiet agent has none.
const colours = {
  slots: new Map([
    ["workflow:w1", "m1"],
    ["agent:a1", "m2"],
  ]),
  subjects: new Map(overview.subjects.map((s) => [s.key, s])),
}
const spec = (change) => ({ ...DEFAULT_SPEC, ...change })
const values = (data, key) => data.rows.map((row) => row[key])

test("a measure with nothing split is one series over the period", () => {
  const data = customData(overview, everything, spec({}), colours)
  assert.deepEqual(data.series.map((s) => s.key), ["value"])
  assert.deepEqual(values(data, "value"), [150, 320, 60])
  assert.equal(data.current, 530)
  assert.equal(data.previous, 100)
  assert.equal(data.series[0].label, "Model tokens")
  assert.deepEqual(data.ranking, [])
  assert.equal(data.rows[0].start, Date.parse("2026-10-05T00:00:00Z"))
})

test("split by type, the kinds come in their fixed order with their own sums", () => {
  const data = customData(overview, everything, spec({ split: "kind" }), colours)
  assert.deepEqual(data.series.map((s) => [s.key, s.label, s.current]), [
    ["workflow", "Workflows", 450],
    ["agent", "Agents", 80],
  ])
  assert.deepEqual(values(data, "agent"), [0, 20, 60])
})

test("split by workflow or agent, series are the ledger's slots and Other", () => {
  const data = customData(
    overview,
    everything,
    spec({ measure: "cost", split: "subject", show: "ranking" }),
    colours
  )
  assert.deepEqual(data.series.map((s) => [s.key, s.label, s.swatch]), [
    ["m1", "Intake", "bg-viz-1"],
    ["m2", "Front desk", "bg-viz-2"],
    ["other", "Other", "bg-chart-3"],
  ])
  // The ranking lists each one, highest first, in its slot's colour.
  assert.deepEqual(
    data.ranking.map((entry) => [entry.label, entry.current, entry.swatch]),
    [
      ["Intake", 3, "bg-viz-1"],
      ["Front desk", 0.5, "bg-viz-2"],
      ["Quiet one", 0.25, "bg-chart-3"],
    ]
  )
})

test("split by model, only model calls count, and models get slots by tokens", () => {
  const data = customData(
    overview,
    everything,
    spec({ measure: "calls", split: "model" }),
    colours
  )
  assert.deepEqual(data.series.map((s) => [s.key, s.label, s.current]), [
    ["m1", "gpt-5", 3],
    ["m2", "claude", 1],
  ])
  assert.deepEqual(values(data, "m1"), [1, 1, 1])
})

test("what can't be drawn as asked falls back", () => {
  // Runs have no model; a ranking needs a split; split rates don't stack.
  assert.equal(drawable(spec({ measure: "started", split: "model" })).split, "none")
  assert.equal(drawable(spec({ show: "ranking" })).show, "total")
  assert.equal(
    drawable(spec({ measure: "successRate", split: "kind", chart: "bars" })).chart,
    "line"
  )
  assert.equal(drawable(spec({ measure: "successRate", chart: "area" })).chart, "area")
  assert.equal(linesOnly(spec({ measure: "successRate", split: "kind" })), true)
  assert.equal(linesOnly(spec({ measure: "cost", split: "kind" })), false)
  const runs = customData(
    overview,
    everything,
    spec({ measure: "started", split: "model" }),
    colours
  )
  assert.deepEqual(runs.series.map((s) => s.key), ["value"])
  assert.equal(runs.current, 6)
})

test("a rate divides its sums, with gaps where there's nothing to divide", () => {
  const data = customData(overview, everything, spec({ measure: "successRate" }), colours)
  assert.deepEqual(values(data, "value"), [0.75, null, 1])
  assert.equal(data.current, 5 / 6)
  assert.equal(data.previous, 0.5)
  // Spend per run takes its spend from model calls and its runs from runs.
  const perRun = customData(overview, everything, spec({ measure: "costPerRun" }), colours)
  assert.equal(perRun.current, 3.75 / 6)
})

test("it counts only the kind it's told to, in the ledger's focus", () => {
  const agents = customData(overview, everything, spec({ only: "agent" }), colours)
  assert.equal(agents.current, 80)
  const focused = customData(
    overview,
    { level: "subject", key: "agent:a1" },
    spec({}),
    colours
  )
  assert.equal(focused.current, 20)
  const none = customData(
    overview,
    { level: "kind", kind: "workflow" },
    spec({ only: "agent" }),
    colours
  )
  assert.equal(none.current, 0)
})

test("a widget's name says what it measures, by what, and whose", () => {
  assert.equal(titleOf(spec({})), "Model tokens")
  assert.equal(
    titleOf(spec({ measure: "cost", split: "subject", only: "agent" })),
    "Model spend by workflow or agent · Agents"
  )
  // As it's drawn: runs by model are runs.
  assert.equal(titleOf(spec({ measure: "failed", split: "model" })), "Failed runs")
})

test("settings read with defaults, and ones that don't fit read as the default", () => {
  assert.deepEqual(DEFAULT_SPEC, {
    measure: "tokens",
    split: "none",
    show: "time",
    chart: "bars",
    only: "all",
  })
  assert.deepEqual(specOf({ measure: "cost" }), spec({ measure: "cost" }))
  assert.deepEqual(specOf({ measure: "nonsense" }), DEFAULT_SPEC)
  assert.equal(CUSTOM_SETTINGS.safeParse({ show: "pie" }).success, false)
})

test("a custom widget goes in a layout with only what it doesn't have by default", () => {
  const layout = [{ id: "custom", type: "custom", x: 0, y: 0, w: 4, h: 100 }]
  const added = withCustomWidget(
    layout,
    spec({ measure: "cost", split: "subject" }),
    "  Model spend by workflow or agent ",
    4
  )
  assert.equal(added.id, "custom-2")
  assert.deepEqual(added.widgets.at(-1), {
    id: "custom-2",
    type: "custom",
    x: 0,
    y: 1,
    w: 4,
    h: 100,
    // Its name already: no title of its own.
    settings: { measure: "cost", split: "subject" },
  })
  const changed = withCustomSpec(added.widgets, "custom-2", DEFAULT_SPEC, "Mine")
  assert.deepEqual(changed.at(-1), {
    id: "custom-2",
    type: "custom",
    x: 0,
    y: 1,
    w: 4,
    h: 100,
    title: "Mine",
  })
})

test("a shared widget is added as a copy, read as its settings allow", () => {
  const copy = withSharedWidget(
    [],
    {
      id: "s1",
      type: "custom",
      title: "Runs by type",
      settings: { measure: "started", split: "kind", show: "pie" },
      shared_by: "mo",
      shared_by_name: "Mo",
      shared_at: "2026-10-16T12:00:00Z",
      can_remove: false,
    },
    4
  )
  // Settings that don't fit any more read as the default widget's.
  assert.deepEqual(copy.widgets, [
    { id: "custom", type: "custom", x: 0, y: 0, w: 4, h: 100, title: "Runs by type" },
  ])
  const fits = withSharedWidget(
    [],
    {
      id: "s2",
      type: "custom",
      title: "Runs by type",
      settings: { measure: "started", split: "kind" },
      shared_by: "mo",
      shared_by_name: "Mo",
      shared_at: "2026-10-16T12:00:00Z",
      can_remove: false,
    },
    4
  )
  assert.deepEqual(fits.widgets[0].settings, { measure: "started", split: "kind" })
  assert.equal(fits.widgets[0].title, "Runs by type")
})
