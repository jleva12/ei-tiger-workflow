import assert from "node:assert/strict"
import { fileURLToPath } from "node:url"
import { test } from "node:test"
import { createServer } from "vite"

// Use the app's existing TS loader without starting a browser or adding a test runtime.
const server = await createServer({
  configFile: false,
  root: fileURLToPath(new URL("..", import.meta.url)),
  server: { middlewareMode: true, watch: null },
  optimizeDeps: { noDiscovery: true, include: [] },
  resolve: { alias: { "@": fileURLToPath(new URL("../src", import.meta.url)) } },
})
// The builder kit's layout and routing, as the ADK workflow builder drives them.
const { AGENT_ADAPTER } = await server.ssrLoadModule("/src/lib/agents/adapter.ts")
const { routeEdges } = await server.ssrLoadModule("/src/lib/builder/routing.ts")
const { edgeId } = await server.ssrLoadModule("/src/lib/builder/types.ts")
const { toGraph } = await server.ssrLoadModule("/src/lib/agents/document.ts")
const { exampleAgent } = await server.ssrLoadModule("/src/lib/agents/example.ts")
const { newNode, outputsOf } = await server.ssrLoadModule("/src/lib/agents/model.ts")
const tidy = (graph, measure) => AGENT_ADAPTER.tidy(graph, measure)
const meaningsOf = (graph, id) => AGENT_ADAPTER.meaningsOf(graph, id)
const loopBodies = (graph) => AGENT_ADAPTER.loopBodies(graph)
await server.close()

// Steps as the canvas draws them: a 64px card, and a 28px row per branch
// below it with its way out at the row's middle.
const WIDTH = 240
function measureOf(graph) {
  const byId = new Map(graph.steps.map((s) => [s.id, s.data]))
  const rows = (id) => {
    const outputs = outputsOf(byId.get(id))
    return outputs.some((o) => o.branch) ? outputs : []
  }
  return {
    size: (id) => ({ width: WIDTH, height: 64 + (rows(id).length ? 8 + rows(id).length * 28 : 0) }),
    port: (id, handle, type) => {
      if (type === "target") return 28
      const index = rows(id).findIndex((o) => o.id === handle)
      return index < 0 ? 28 : 64 + 4 + 14 + index * 28
    },
  }
}

/** Tidies a graph and routes its connections the way the canvas does. */
function draw(graph) {
  const measure = measureOf(graph)
  const positions = tidy(graph, measure)
  const boxes = new Map(
    graph.steps.map((s) => [s.id, { ...positions.get(s.id), ...measure.size(s.id) }])
  )
  const meanings = meaningsOf(graph, (c) => edgeId(c.source, c.output, c.target))
  const requests = graph.connections.map((c) => {
    const id = edgeId(c.source, c.output, c.target)
    const meaning = meanings.get(id)
    const source = boxes.get(c.source)
    const target = boxes.get(c.target)
    return {
      id,
      source: c.source,
      target: c.target,
      from: {
        x: source.x + source.width + 5,
        y: source.y + measure.port(c.source, c.output, "source"),
      },
      to: { x: target.x - 5, y: target.y + 28 },
      loop: meaning.role === "return" ? meaning.loop : undefined,
      bundle: meaning.tone,
      label: meaning.label && { text: meaning.label, always: meaning.role !== "branch" },
    }
  })
  const started = performance.now()
  const routes = routeEdges(boxes, requests, loopBodies(graph))
  return { boxes, routes, requests, meanings, ms: performance.now() - started }
}

function build(steps, connections) {
  return {
    steps: steps.map(([id, kind, patch = {}]) => {
      const data = newNode(kind)
      return {
        id,
        data: { ...data, config: { ...data.config, ...patch } },
        position: { x: 0, y: 0 },
      }
    }),
    connections: connections.map(([source, output, target]) => ({ source, output, target })),
  }
}

/** Whether a label (its centre and width, 18px tall) lies over a box. */
const covers = (label, box) =>
  label.x - label.width / 2 < box.x + box.width &&
  label.x + label.width / 2 > box.x &&
  label.y - 9 < box.y + box.height &&
  label.y + 9 > box.y

function assertDrawnCleanly({ boxes, routes, requests }) {
  const list = [...boxes]
  for (let i = 0; i < list.length; i += 1) {
    for (let j = i + 1; j < list.length; j += 1) {
      const [a, A] = list[i]
      const [b, B] = list[j]
      const apart =
        A.x + A.width + 16 <= B.x ||
        B.x + B.width + 16 <= A.x ||
        A.y + A.height + 16 <= B.y ||
        B.y + B.height + 16 <= A.y
      assert.ok(apart, `${a} and ${b} overlap`)
    }
  }
  for (const request of requests) {
    const { points } = routes.get(request.id)
    for (let i = 1; i < points.length; i += 1) {
      const a = points[i - 1]
      const b = points[i]
      assert.ok(a.x === b.x || a.y === b.y, `${request.id} has a slanted stretch`)
      for (const [id, box] of boxes) {
        if (
          (i === 1 && id === request.source) ||
          (i === points.length - 1 && id === request.target)
        )
          continue
        const through =
          Math.min(a.x, b.x) < box.x + box.width &&
          Math.max(a.x, b.x) > box.x &&
          Math.min(a.y, b.y) < box.y + box.height &&
          Math.max(a.y, b.y) > box.y
        assert.ok(!through, `${request.id} runs through ${id}`)
      }
    }
  }
  const shown = [...routes.values()].filter((r) => r.labelShown).map((r) => r.label)
  for (const label of shown) {
    for (const [id, box] of boxes) assert.ok(!covers(label, box), `a label covers ${id}`)
  }
}

test("the example ADK workflow tidies with no line through a step", () => {
  assertDrawnCleanly(draw(toGraph(exampleAgent("org"))))
})

// Not yet: the tidy lines a loop agent (polish) up 54px off the step it leads to (replied).
test("the example ADK workflow tidies with straight runs", { todo: "polish → replied bends" }, () => {
  const graph = toGraph(exampleAgent("org"))
  const drawn = draw(graph)
  // A step's only way out runs straight into a step that only it leads to.
  const into = new Map()
  for (const c of graph.connections) into.set(c.target, (into.get(c.target) ?? 0) + 1)
  for (const c of graph.connections) {
    const source = graph.steps.find((s) => s.id === c.source)
    if (outputsOf(source.data).length !== 1 || into.get(c.target) !== 1) continue
    const { points } = drawn.routes.get(edgeId(c.source, c.output, c.target))
    assert.equal(points.length, 2, `${c.source} → ${c.target} bends`)
  }
})

test("a loop's way back runs over its body and comes down into the loop", () => {
  const graph = build(
    [
      ["start", "start"],
      ["files", "loop", { items: "input.files" }],
      ["read", "http"],
      ["keep", "transform"],
      ["miss", "transform"],
      ["pages", "loop", { items: "steps.keep.output" }],
      ["page", "transform"],
      ["done", "end"],
    ],
    [
      ["start", "next", "files"],
      ["files", "each", "read"],
      ["read", "success", "keep"],
      ["read", "error", "miss"],
      ["keep", "next", "pages"],
      ["pages", "each", "page"],
      ["page", "next", "pages"],
      ["pages", "done", "files"],
      ["miss", "next", "files"],
      ["files", "done", "done"],
    ]
  )
  const drawn = draw(graph)
  assertDrawnCleanly(drawn)
  const bodies = loopBodies(graph)
  assert.deepEqual([...bodies.get("files")].sort(), ["keep", "miss", "page", "pages", "read"])
  for (const [loop, returning] of [
    ["files", ["pages:done->files", "miss:next->files"]],
    ["pages", ["page:next->pages"]],
  ]) {
    const box = drawn.boxes.get(loop)
    const top = Math.min(box.y, ...[...bodies.get(loop)].map((id) => drawn.boxes.get(id).y))
    for (const id of returning) {
      assert.equal(drawn.meanings.get(id).role, "return")
      const { points } = drawn.routes.get(id)
      const end = points.at(-1)
      assert.deepEqual(end, { x: box.x + box.width / 2, y: box.y }, `${id} ends on ${loop}'s top`)
      assert.ok(points.at(-2).y < end.y, `${id} comes down into ${loop}`)
      assert.ok(Math.min(...points.map((p) => p.y)) < top, `${id} runs over ${loop}'s body`)
    }
  }
  // The inner loop's lane runs under the outer loop's.
  const lane = (id) => Math.min(...drawn.routes.get(id).points.map((p) => p.y))
  assert.ok(lane("page:next->pages") > lane("miss:next->files"))
})

test("a wide switch fans out in its rows' order and its lines keep apart", () => {
  const cases = ["a", "b", "c", "d", "e", "f"].map((value, i) => ({ id: `case_${i}`, value }))
  const graph = build(
    [
      ["start", "start"],
      ["route", "switch", { value: "input.kind", cases }],
      ...cases.map((c) => [`do_${c.id}`, "transform"]),
      ["join", "merge"],
      ["end", "end"],
    ],
    [
      ["start", "next", "route"],
      ...cases.map((c) => ["route", c.id, `do_${c.id}`]),
      ["route", "default", "join"],
      ...cases.map((c) => [`do_${c.id}`, "next", "join"]),
      ["join", "next", "end"],
    ]
  )
  const drawn = draw(graph)
  assertDrawnCleanly(drawn)
  const ys = cases.map((c) => drawn.boxes.get(`do_${c.id}`).y)
  assert.deepEqual(
    ys,
    [...ys].sort((a, b) => a - b),
    "cases stand top to bottom in row order"
  )
  // Lines that bend in the same gap don't share an upright stretch.
  const uprights = []
  for (const request of drawn.requests) {
    const { points } = drawn.routes.get(request.id)
    for (let i = 1; i < points.length; i += 1) {
      if (points[i].x === points[i - 1].x && points[i].y !== points[i - 1].y) {
        uprights.push({
          request,
          x: points[i].x,
          lo: Math.min(points[i].y, points[i - 1].y),
          hi: Math.max(points[i].y, points[i - 1].y),
        })
      }
    }
  }
  for (let i = 0; i < uprights.length; i += 1) {
    for (let j = i + 1; j < uprights.length; j += 1) {
      const a = uprights[i]
      const b = uprights[j]
      const shared = a.request.target === b.request.target && a.request.bundle === b.request.bundle
      if (shared || Math.abs(a.x - b.x) > 1) continue
      assert.ok(a.hi <= b.lo || b.hi <= a.lo, `${a.request.id} and ${b.request.id} overlap`)
    }
  }
  // Each case keeps its own colour.
  const tones = cases.map((c) => drawn.meanings.get(edgeId("route", c.id, `do_${c.id}`)).tone)
  assert.deepEqual(tones.slice(0, 4), ["case-1", "case-2", "case-3", "case-4"])
  assert.equal(drawn.meanings.get(edgeId("route", "default", "join")).tone, "neutral")
})

test("routing a large ADK workflow stays quick", () => {
  const steps = [["start", "start"]]
  const connections = []
  let previous = "start"
  for (let i = 0; i < 20; i += 1) {
    steps.push(
      [`check_${i}`, "if"],
      [`yes_${i}`, "http"],
      [`no_${i}`, "transform"],
      [`join_${i}`, "merge"]
    )
    connections.push(
      [previous, "next", `check_${i}`],
      [`check_${i}`, "true", `yes_${i}`],
      [`check_${i}`, "false", `no_${i}`],
      [`yes_${i}`, "success", `join_${i}`],
      [`yes_${i}`, "error", `no_${i}`],
      [`no_${i}`, "next", `join_${i}`]
    )
    previous = `join_${i}`
  }
  const drawn = draw(build(steps, connections))
  assertDrawnCleanly(drawn)
  assert.ok(drawn.ms < 150, `routing took ${drawn.ms.toFixed(1)}ms`)
  console.log(`routed ${drawn.requests.length} connections in ${drawn.ms.toFixed(1)}ms`)
})
