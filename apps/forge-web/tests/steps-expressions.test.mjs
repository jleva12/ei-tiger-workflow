import assert from "node:assert/strict"
import { readFile } from "node:fs/promises"
import { fileURLToPath } from "node:url"
import { test } from "node:test"
import jsonata from "jsonata"
import { createServer } from "vite"

// Use the app's existing TS loader without starting a browser or adding a test runtime.
const server = await createServer({
  configFile: false,
  root: fileURLToPath(new URL("..", import.meta.url)),
  server: { middlewareMode: true, watch: null },
  optimizeDeps: { noDiscovery: true, include: [] },
  resolve: { alias: { "@": fileURLToPath(new URL("../src", import.meta.url)) } },
})
const { checkField, completeField, resolveExpression, typeAt } = await server.ssrLoadModule("/src/lib/steps/expressions.ts")
const { normalizeReferences } = await server.ssrLoadModule("/src/lib/steps/references.ts")
const { t, valuesOf } = await server.ssrLoadModule("/src/lib/steps/types.ts")
const { fieldOfCode, switchIssues } = await server.ssrLoadModule("/src/lib/steps/validate.ts")
await server.close()

const fixtures = JSON.parse(await readFile(new URL("../../../packages/python/tasks/adk-workflows/tests/fixtures/expression-references.json", import.meta.url)))
const output = t.object({ count: t.number(), priority: t.string(undefined, { enum: ["high", "low"] }) })
const steps = t.object({ measure: t.object({ output }) })
const scope = {
  roots: new Map([
    ["input", { name: "input", type: t.object({ limit: t.number() }), detail: "Input" }],
    ["previous", { name: "previous", type: output, detail: "Previous output" }],
    ["steps", { name: "steps", type: steps, detail: "Other steps" }],
  ]),
  steps: new Map([["measure", { id: "measure", name: "Measure", kind: "transform", mark: { label: "Transform" }, output }]]),
  allSteps: new Map([["measure", { name: "Measure" }]]),
}

for (const { expression, value } of fixtures.cases) {
  test(`reference semantics: ${expression}`, async () => {
    const normalized = normalizeReferences(expression)
    assert.equal(normalized.length, expression.length, "editor offsets must stay intact")
    assert.deepEqual(JSON.parse(JSON.stringify(await jsonata(normalized).evaluate(fixtures.data))), value)
  })
}

for (const expression of fixtures.invalid) {
  test(`validation rejects ${expression}`, () => {
    assert.ok(checkField(expression, "expression", scope).some((d) => d.severity === "error"))
  })
}

test("conditions validate and infer types with wrapped and bare references", () => {
  for (const expression of ["{{ steps.measure.output.count }} > 9", "previous.count > 9"]) {
    assert.deepEqual(checkField(expression, "expression", scope), [])
    assert.equal(resolveExpression(expression, scope).kind, "boolean")
  }
  assert.equal(resolveExpression("{{ previous.count }}", scope).kind, "number")
})

test("autocomplete offers roots, other nodes and their fields inside references", () => {
  for (const [text, label] of [["{{ ", "steps"], ["{{ steps.", "measure"], ["{{ steps.measure.output.", "count"]]) {
    const result = completeField(text, text.length, "expression", scope)
    assert.equal(result.from, text.length)
    assert.ok(result.options.some((option) => option.label === label))
  }
  const text = "{{ steps.measure.output.co }} > 9"
  const caret = text.indexOf("co ") + 2
  const result = completeField(text, caret, "expression", scope)
  assert.equal(text.slice(result.from, result.to), "co")
  assert.ok(result.options.some((option) => option.label === "count"))
})

test("autocomplete suggests allowed values after a wrapped reference", () => {
  const text = '{{ previous.priority }} = "'
  assert.deepEqual(completeField(text, text.length, "expression", scope).options.map((o) => o.label), ["high", "low"])
})

test("hover and errors point into the original reference", () => {
  const good = "{{ steps.measure.output.count }} > 9"
  const at = good.indexOf("count")
  const hover = typeAt(good, at + 1, "expression", scope)
  assert.equal(hover.label, "number")
  assert.equal(good.slice(hover.from, hover.to), "count")
  const bad = "{{ previous.missing }} > 9"
  const [error] = checkField(bad, "expression", scope)
  assert.equal(bad.slice(error.from, error.to), "missing")
})

test("text templates retain their existing behavior", () => {
  assert.deepEqual(checkField("Count: {{ previous.count }}", "template", scope), [])
  assert.equal(completeField("Count: ", 7, "template", scope), null)
})

test("a switch's cases are held to the values what it switches on can have", () => {
  assert.deepEqual(valuesOf(resolveExpression("previous.priority", scope)), ["high", "low"])
  assert.deepEqual(valuesOf(resolveExpression("previous.count > 9", scope)), ["true", "false"])
  assert.equal(valuesOf(resolveExpression("previous.count", scope)), undefined)
  const step = (value, cases) => ({
    kind: "switch",
    name: "Route",
    config: { value, cases: cases.map((v, i) => ({ id: `c${i}`, value: v })) },
  })
  const found = switchIssues(step("previous.priority", ["high", "medium", ""]), scope)
  assert.deepEqual(found.map(([code, level]) => [code, level]), [["case-value-c1", "error"]])
  assert.match(found[0][2], /"medium" could never be taken: previous.priority is high or low/)
  // Any value can come: any case can be taken.
  assert.deepEqual(switchIssues(step("previous.count", ["3"]), scope), [])
})

test("a step's issue names the setting its field shows it under", () => {
  assert.equal(fieldOfCode("url"), "url")
  assert.equal(fieldOfCode("item-name"), "item_name")
  assert.equal(fieldOfCode("case-value-c1"), "cases.c1")
  assert.equal(fieldOfCode("field:headers.h_ct:0"), "headers.h_ct")
  assert.equal(fieldOfCode("field:arms.rule_1:2"), "arms.rule_1")
  // A way that goes nowhere is the step's, not a setting's.
  assert.equal(fieldOfCode("open-true"), undefined)
})
