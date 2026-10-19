import assert from "node:assert/strict"
import { fileURLToPath } from "node:url"
import { test } from "node:test"
import { createServer } from "vite"
import { z } from "zod"

const server = await createServer({
  configFile: false,
  root: fileURLToPath(new URL("..", import.meta.url)),
  resolve: { alias: { "@": fileURLToPath(new URL("../src", import.meta.url)) } },
  server: { middlewareMode: true, watch: null },
  optimizeDeps: { noDiscovery: true, include: [] },
})
const {
  FIT_HEIGHT,
  GRID_COLUMNS,
  LAYOUT_VERSION,
  OVERVIEW_LAYOUT,
  OVERVIEW_WIDGET_TYPES,
  TITLE_LENGTH,
  arranged,
  bottomOf,
  inReadingOrder,
  resolveLayout,
  retitled,
  sameWidgets,
  settle,
  withSetting,
  withSettings,
  withWidget,
  withoutWidget,
} = await server.ssrLoadModule("/src/features/overview/lib/dashboard.ts")
const { CHART_TYPES, chartTypeOf } = await server.ssrLoadModule(
  "/src/features/overview/lib/chart-type.ts"
)
await server.close()

const catalog = {
  chart: {
    minW: 4,
    settings: z.object({
      split: z.enum(["type", "subject"]).default("type"),
      stacked: z.boolean().default(true),
    }),
  },
  list: {},
}
const fallback = {
  version: LAYOUT_VERSION,
  widgets: [{ id: "chart", type: "chart", x: 0, y: 0, w: 12, h: FIT_HEIGHT }],
}
const widget = (id, x, y, w, h = FIT_HEIGHT, type = "list") => ({
  id,
  type,
  x,
  y,
  w,
  h,
})
const at = (placed) =>
  Object.fromEntries(placed.map(({ i, x, y, w, h }) => [i, [x, y, w, h]]))

test("no saved layout, or one of another version, draws the default", () => {
  assert.equal(resolveLayout(undefined, catalog, fallback), fallback)
  assert.equal(resolveLayout(null, catalog, fallback), fallback)
  assert.equal(resolveLayout("layout", catalog, fallback), fallback)
  assert.equal(
    resolveLayout({ version: LAYOUT_VERSION + 1, widgets: [] }, catalog, fallback),
    fallback
  )
  assert.equal(
    resolveLayout({ version: LAYOUT_VERSION, widgets: {} }, catalog, fallback),
    fallback
  )
})

test("a saved layout keeps what fits and leaves out what doesn't", () => {
  const resolved = resolveLayout(
    {
      version: LAYOUT_VERSION,
      widgets: [
        { id: "a", type: "list", x: 2.4, y: 10, w: 3.6, h: 250, title: "  Mine  " },
        { id: "a", type: "list", x: 0, y: 0, w: 4, h: 100 },
        { id: "gone", type: "retired", x: 0, y: 0, w: 4, h: 100 },
        { id: "b", type: "chart", x: 11, y: -5, w: 2, h: 9999, title: " " },
        { id: "c", type: "list", x: "left", y: null, w: 40 },
        { id: "", type: "list", x: 0, y: 0, w: 4, h: 100 },
        { id: "proto", type: "toString", x: 0, y: 0, w: 4, h: 100 },
        "not a widget",
      ],
    },
    catalog,
    fallback
  )
  assert.deepEqual(resolved, {
    version: LAYOUT_VERSION,
    widgets: [
      { id: "a", type: "list", x: 2, y: 10, w: 4, h: 250, title: "Mine" },
      // Widened to its type's least width, kept on the grid and below the
      // cap; its settings are its type's defaults, so it keeps none.
      { id: "b", type: "chart", x: 8, y: 0, w: 4, h: 4000 },
      { id: "c", type: "list", x: 0, y: 0, w: GRID_COLUMNS, h: FIT_HEIGHT },
    ],
  })
})

test("a widget keeps only the settings that aren't its type's defaults", () => {
  const settingsOf = (settings) =>
    resolveLayout(
      {
        version: LAYOUT_VERSION,
        widgets: [{ id: "a", type: "chart", x: 0, y: 0, w: 6, h: 100, settings }],
      },
      catalog,
      fallback
    ).widgets[0].settings
  assert.deepEqual(settingsOf({ split: "subject" }), { split: "subject" })
  assert.deepEqual(settingsOf({ split: "subject", stacked: false }), {
    split: "subject",
    stacked: false,
  })
  // Any that doesn't fit puts them all back to the defaults.
  assert.equal(settingsOf({ split: "bogus", stacked: false }), undefined)
  assert.equal(settingsOf({ split: "type", stacked: true }), undefined)
  assert.equal(settingsOf("nope"), undefined)
  // A type without settings keeps none.
  const list = resolveLayout(
    {
      version: LAYOUT_VERSION,
      widgets: [{ id: "a", type: "list", x: 0, y: 0, w: 6, h: 100, settings: { a: 1 } }],
    },
    catalog,
    fallback
  )
  assert.equal(list.widgets[0].settings, undefined)
})

test("the overview's default is its read-out and ledger, then rows of panels", () => {
  assert.deepEqual(
    OVERVIEW_LAYOUT.widgets.map((w) => w.type).sort(),
    [...OVERVIEW_WIDGET_TYPES].sort()
  )
  const heights = new Map([
    ["readout", 96],
    ["ledger", 900],
    ["tokens", 425.3],
    ["models", 393],
    ["activity", 406],
    ["outcomes", 420],
    ["spend", 525],
    ["people", 341],
    ["steps", 505],
    ["attention", 328],
  ])
  assert.deepEqual(at(settle(OVERVIEW_LAYOUT.widgets, heights)), {
    readout: [0, 0, 12, 96],
    ledger: [0, 96, 12, 900],
    tokens: [0, 996, 8, 426],
    models: [8, 996, 4, 426],
    activity: [0, 1422, 4, 525],
    outcomes: [4, 1422, 4, 525],
    spend: [8, 1422, 4, 525],
    people: [0, 1947, 4, 505],
    steps: [4, 1947, 4, 505],
    attention: [8, 1947, 4, 505],
  })
})

test("a layout from before the read-out and ledger were widgets gets them on top", () => {
  const catalog = Object.fromEntries(OVERVIEW_WIDGET_TYPES.map((type) => [type, {}]))
  const resolved = resolveLayout(
    {
      version: 1,
      widgets: [
        widget("activity", 0, 0, 4, FIT_HEIGHT, "activity"),
        widget("tokens", 4, 483, 8, FIT_HEIGHT, "tokens"),
      ],
    },
    catalog,
    OVERVIEW_LAYOUT
  )
  assert.equal(resolved.version, LAYOUT_VERSION)
  assert.deepEqual(
    resolved.widgets.map(({ id, x, y, w }) => [id, x, y, w]),
    [
      ["readout", 0, 0, GRID_COLUMNS],
      ["ledger", 0, 1, GRID_COLUMNS],
      ["activity", 0, 2, 4],
      ["tokens", 4, 485, 8],
    ]
  )
  // Drawn, they keep their order under the two.
  const placed = settle(resolved.widgets, new Map([["readout", 90], ["ledger", 600]]))
  assert.deepEqual(
    inReadingOrder(placed).map((item) => item.i),
    ["readout", "ledger", "activity", "tokens"]
  )
})

test("widgets rise to what's above them in their columns", () => {
  const placed = settle(
    [widget("top", 0, 0, 6), widget("far", 0, 5000, 6), widget("side", 6, 900, 6)],
    new Map([
      ["top", 200],
      ["far", 150],
      ["side", 120],
    ])
  )
  assert.deepEqual(at(placed), {
    top: [0, 0, 6, 200],
    side: [6, 0, 6, 200],
    far: [0, 200, 6, 150],
  })
})

test("a widget is its layout height or its content's, whichever is more", () => {
  const placed = settle(
    [widget("tall", 0, 0, 6, 600), widget("short", 6, 0, 6, 100)],
    new Map([
      ["tall", 300],
      ["short", 250],
    ])
  )
  assert.deepEqual(at(placed), { tall: [0, 0, 6, 600], short: [6, 0, 6, 250] })
})

test("only widgets on one line with one layout height stretch as a row", () => {
  // A tall widget beside two stacked ones leaves them their own heights.
  const placed = settle(
    [
      widget("tall", 0, 0, 8, 600),
      widget("upper", 8, 0, 4, 100),
      widget("lower", 8, 1, 4, 100),
      widget("under", 0, 2, 12),
    ],
    new Map([
      ["tall", 300],
      ["upper", 280],
      ["lower", 290],
      ["under", 150],
    ])
  )
  assert.deepEqual(at(placed), {
    tall: [0, 0, 8, 600],
    upper: [8, 0, 4, 280],
    lower: [8, 280, 4, 290],
    under: [0, 600, 12, 150],
  })
  assert.equal(bottomOf(placed), 750)
})

test("a row that grows pushes the rows below it down", () => {
  const placed = settle(
    [
      widget("a", 0, 0, 6),
      widget("b", 6, 0, 6),
      widget("c", 0, 1, 4),
      widget("d", 4, 1, 8),
      widget("e", 0, 2, 12),
    ],
    new Map([
      ["a", 120],
      ["b", 300],
      ["c", 200],
      ["d", 110],
      ["e", 130],
    ])
  )
  assert.deepEqual(at(placed), {
    a: [0, 0, 6, 300],
    b: [6, 0, 6, 300],
    c: [0, 300, 4, 200],
    d: [4, 300, 8, 200],
    e: [0, 500, 12, 130],
  })
})

test("placed widgets stack top to bottom, left to right", () => {
  const order = inReadingOrder([
    { i: "c", x: 0, y: 400, w: 12, h: 10 },
    { i: "b", x: 6, y: 0, w: 6, h: 10 },
    { i: "a", x: 0, y: 0, w: 6, h: 10 },
  ]).map((item) => item.i)
  assert.deepEqual(order, ["a", "b", "c"])
})

test("a drag moves widgets and keeps their sizes", () => {
  const widgets = [widget("a", 0, 0, 6), widget("b", 6, 0, 6, 300)]
  // react-grid-layout reports drawn heights; they aren't least heights.
  const moved = arranged(widgets, [
    { i: "a", x: 6, y: 420, w: 6, h: 420 },
    { i: "b", x: 0, y: 0, w: 6, h: 420 },
  ])
  assert.deepEqual(moved, [widget("a", 6, 420, 6), widget("b", 0, 0, 6, 300)])
})

test("a resize keeps the height unless the height was dragged", () => {
  const widgets = [widget("a", 0, 0, 6), widget("b", 6, 0, 6)]
  const from = { i: "a", x: 0, y: 0, w: 6, h: 380 }
  const wider = arranged(
    widgets,
    [{ ...from, w: 8 }, { i: "b", x: 8, y: 0, w: 6, h: 380 }],
    { from, to: { ...from, w: 8 } }
  )
  assert.deepEqual(wider[0], widget("a", 0, 0, 8))
  // Kept on the grid, whatever was reported.
  assert.deepEqual(wider[1], widget("b", 6, 0, 6))
  const taller = arranged(widgets, [{ ...from, w: 4, h: 520 }], {
    from,
    to: { ...from, w: 4, h: 520 },
  })
  assert.deepEqual(taller[0], widget("a", 0, 0, 4, 520))
  const huge = arranged(widgets, [{ ...from, h: 9000 }], {
    from,
    to: { ...from, h: 9000 },
  })
  assert.equal(huge[0].h, 4000)
})

test("an added widget goes under the rest, with an ID of its own", () => {
  const widgets = [widget("tokens", 0, 0, 8, FIT_HEIGHT, "tokens"), widget("b", 8, 950, 4)]
  const once = withWidget(widgets, "models", 4)
  assert.deepEqual(once.at(-1), widget("models", 0, 951, 4, FIT_HEIGHT, "models"))
  const twice = withWidget(widgets, "tokens", 20)
  assert.deepEqual(twice.at(-1), {
    ...widget("tokens-2", 0, 951, GRID_COLUMNS),
    type: "tokens",
  })
  assert.deepEqual(withWidget([], "models", 4), [
    widget("models", 0, 0, 4, FIT_HEIGHT, "models"),
  ])
  assert.deepEqual(withoutWidget(once, "b").map((w) => w.id), ["tokens", "models"])
})

test("a widget's own title is trimmed, and a blank one puts its type's back", () => {
  const widgets = [widget("a", 0, 0, 6), widget("b", 6, 0, 6)]
  const titled = retitled(widgets, "a", "  Spend this week ")
  assert.equal(titled[0].title, "Spend this week")
  assert.equal(titled[1].title, undefined)
  assert.equal(retitled(titled, "a", "x".repeat(200))[0].title.length, TITLE_LENGTH)
  const plain = retitled(titled, "a", "   ")
  assert.equal("title" in plain[0], false)
})

test("layouts are the same when their widgets are, in any order", () => {
  const a = [widget("a", 0, 0, 6), widget("b", 6, 0, 6)]
  assert.equal(sameWidgets(a, [...a].reverse()), true)
  assert.equal(sameWidgets(a, [a[0], { ...a[1], x: 0, y: 1 }]), false)
  assert.equal(sameWidgets(a, retitled(a, "b", "B")), false)
  assert.equal(sameWidgets(a, a.slice(1)), false)
})

test("a setting changed, and left out when it's set back to its default", () => {
  const widgets = [widget("a", 0, 0, 6, FIT_HEIGHT, "chart"), widget("b", 6, 0, 6)]
  const lines = withSetting(widgets, "a", "chart", "line", "bars")
  assert.deepEqual(lines[0].settings, { chart: "line" })
  assert.equal(lines[1], widgets[1])
  const back = withSetting(lines, "a", "chart", "bars", "bars")
  assert.equal("settings" in back[0], false)
  // Back to the default, the layout is the one it was.
  assert.equal(sameWidgets(back, widgets), true)
  const both = withSetting(
    withSetting(widgets, "a", "chart", "area", "bars"),
    "a",
    "split",
    "subject",
    "type"
  )
  assert.deepEqual(both[0].settings, { chart: "area", split: "subject" })
})

test("a widget's chart type is bars unless its settings pick another", () => {
  assert.deepEqual([...CHART_TYPES], ["bars", "area", "line"])
  assert.equal(chartTypeOf({}), "bars")
  assert.equal(chartTypeOf({ chart: "line" }), "line")
  assert.equal(chartTypeOf({ chart: "area" }), "area")
  assert.equal(chartTypeOf({ chart: "pie" }), "bars")
  assert.equal(chartTypeOf({ chart: ["line"] }), "bars")
})

test("several settings change at once, keeping only those off their defaults", () => {
  const widgets = [widget("c", 0, 0, 4, FIT_HEIGHT, "custom")]
  const defaults = { measure: "tokens", split: "none", show: "time" }
  const set = withSettings(widgets, "c", { measure: "cost", split: "none", show: "ranking" }, defaults)
  assert.deepEqual(set[0].settings, { measure: "cost", show: "ranking" })
  const back = withSettings(set, "c", defaults, defaults)
  assert.equal("settings" in back[0], false)
})
