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
const { lookOf, familyOf, mergeGraph, visibleGraph, nodeLabel, propertyValue, typeCounts, MAX_NODES } =
  await server.ssrLoadModule("/src/features/code-graph/lib/graph.ts")
await server.close()

const node = (id, kind = "method", more = {}) => ({
  fact: { node: { id, kind, name: id, ...more } },
  gen_from: 1,
  commit_from: "abc",
})
const edge = (id, source_id, target_id, kind = "calls") => ({
  fact: { edge: { id, kind, source_id, target_id } },
  gen_from: 1,
  commit_from: "abc",
})
const graph = (nodes, edges = [], more = {}) => ({
  repository_id: "repo",
  generation: 3,
  nodes,
  edges,
  truncated: false,
  ...more,
})

test("a known kind is drawn as its family, with its glyph", () => {
  assert.equal(lookOf("class").family, "type")
  assert.deepEqual(lookOf("class").glyph, { text: "C" })
  assert.deepEqual(lookOf("source_file").glyph, { icon: "file" })
  assert.equal(lookOf("unresolved_reference").origin, "unresolved")
})

test("a derived kind is its base kind, dashed; an unknown one is other", () => {
  assert.equal(lookOf("derived_method").family, "callable")
  assert.equal(lookOf("derived_method").origin, "derived")
  assert.equal(familyOf("widget"), "other")
  assert.deepEqual(lookOf("Widget").glyph, { text: "w" })
  // Kinds are data: Object.prototype's names aren't kinds.
  assert.equal(lookOf("toString").family, "other")
  assert.equal(lookOf("constructor").family, "callable")
})

test("merging adds new nodes and edges between loaded nodes only", () => {
  const merged = mergeGraph(
    graph([node("a")]),
    graph([node("a"), node("b")], [edge("ab", "a", "b"), edge("ax", "a", "x")])
  )
  assert.deepEqual(
    merged.nodes.map((v) => v.fact.node.id),
    ["a", "b"]
  )
  assert.deepEqual(
    merged.edges.map((v) => v.fact.edge.id),
    ["ab"]
  )
  // The edge to a node not loaded was left out.
  assert.equal(merged.truncated, true)
})

test("merging stops at the canvas's node cap and says it's truncated", () => {
  const full = graph(Array.from({ length: MAX_NODES }, (_, i) => node(`n${i}`)))
  const merged = mergeGraph(full, graph([node("extra"), node("n0")]))
  assert.equal(merged.nodes.length, MAX_NODES)
  assert.equal(merged.truncated, true)
})

test("merging refuses another generation", () => {
  assert.throws(() => mergeGraph(graph([]), graph([], [], { generation: 4 })), /changed/)
})

test("the visible graph follows the kind toggles and the text filter", () => {
  const data = graph(
    [node("parse", "method"), node("Parser", "class"), node("x", "field", { qualified_name: "pkg.Parser.x" })],
    [edge("e1", "Parser", "parse", "contains"), edge("e2", "parse", "x", "reads")]
  )
  const byKind = visibleGraph(data, new Set(["field"]), new Set(), "")
  assert.deepEqual(
    byKind.nodes.map((v) => v.fact.node.id),
    ["parse", "Parser"]
  )
  assert.deepEqual(
    byKind.edges.map((v) => v.fact.edge.id),
    ["e1"]
  )
  const byText = visibleGraph(data, new Set(), new Set(["contains"]), " PARSER ")
  assert.deepEqual(
    byText.nodes.map((v) => v.fact.node.id),
    ["Parser", "x"]
  )
  assert.deepEqual(byText.edges, [])
})

test("labels, property values and kind counts", () => {
  assert.equal(nodeLabel({ id: "n1", kind: "method", qualified_name: "a.b" }), "a.b")
  assert.equal(nodeLabel({ id: "n1", kind: "method" }), "n1")
  assert.equal(propertyValue({ int64: 0 }), 0)
  assert.equal(propertyValue({ bool: false }), false)
  assert.equal(propertyValue({ strings: ["a", "b"] }), "a, b")
  assert.deepEqual(typeCounts(["method", "class", "method"]), [
    ["class", 1],
    ["method", 2],
  ])
})
