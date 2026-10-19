import assert from "node:assert/strict"
import { test } from "node:test"
import { fileURLToPath } from "node:url"
import { createElement } from "react"
import { renderToStaticMarkup } from "react-dom/server"
import { renderGenerativeUI } from "@assistant-ui/generative-ui/react"
import { createServer } from "vite"

const server = await createServer({
  configFile: false,
  root: fileURLToPath(new URL("..", import.meta.url)),
  resolve: {
    alias: { "@": fileURLToPath(new URL("../src", import.meta.url)) },
  },
  server: { middlewareMode: true, hmr: false, watch: null },
  optimizeDeps: { noDiscovery: true, include: [] },
})
const load = (name) =>
  server.ssrLoadModule(`/src/components/forge/generative-ui/${name}.ts`)
let LIBRARY, AnswerContext, presentCatalog, vocabulary, withCardForms, withQuietFields
try {
  ;({ LIBRARY } = await load("library"))
  ;({ AnswerContext } = await load("answer-context"))
  ;({ presentCatalog, RICH_ANSWER_VOCABULARY: vocabulary } = await load("catalog"))
  ;({ withCardForms, withQuietFields } = await load("composition"))
} finally {
  await server.close()
}

function render(tree, answer = {}) {
  const context = { live: true, send: () => {}, ...answer }
  return renderToStaticMarkup(
    createElement(
      AnswerContext.Provider,
      { value: context },
      renderGenerativeUI(withQuietFields(withCardForms(tree)), LIBRARY, {
        status: "done",
        ...(context.live && { dispatch: () => {} }),
      })
    )
  )
}

test("every component offered to the agent has a renderer with the same schema", () => {
  const catalog = presentCatalog()
  assert.deepEqual(Object.keys(LIBRARY), Object.keys(catalog.components))
  for (const [name, entry] of Object.entries(LIBRARY)) {
    assert.equal(typeof entry.render, "function", name)
    assert.equal(entry.description, catalog.components[name].description, name)
    assert.equal(entry.properties, vocabulary[name].properties, name)
  }
})

test("custom display components retain formatting and accessible chart data", () => {
  const html = render({
    _type: "Col",
    children: [
      {
        _type: "Fact",
        label: "Orders",
        value: "123",
        trend: "up",
        delta: "12%",
      },
      {
        _type: "Table",
        columns: [
          { label: "City" },
          { label: "Total", format: { kind: "currency", currency: "USD" } },
        ],
        rows: [
          ["Boston", 10],
          ["New York", 20],
        ],
        sortable: true,
      },
      {
        _type: "Steps",
        steps: [{ title: "Book", detail: "Pick a day" }, { title: "Confirm" }],
      },
      {
        _type: "Chart",
        series: [
          { label: "Orders", data: [{ label: "Q1", value: 10 }] },
          { label: "Returns", data: [{ label: "Q1", value: 2 }] },
        ],
      },
    ],
  })
  assert.match(html, /data-slot="rich-fact"/)
  assert.match(html, /123/)
  assert.match(html, /, better/)
  assert.match(html, /\$10\.00/)
  assert.match(html, /type="button"[^>]*>Total<\/button>/)
  assert.match(html, /Pick a day/)
  assert.match(html, /<table class="sr-only">/)
  assert.match(html, /<th scope="col">Returns<\/th>/)
  assert.match(html, /<th scope="row">Q1<\/th><td>10<\/td><td>2<\/td>/)
})

test("choices stay usable while live and preserve the selected reply when answered", () => {
  const tree = {
    _type: "Choices",
    options: [{ label: "Yes", reply: "Confirm" }, { label: "No" }],
  }
  const live = render(tree)
  assert.match(live, /role="group" aria-label="Choices"/)
  assert.doesNotMatch(live, /disabled=""|aria-pressed=/)
  const answered = render(tree, { live: false, picked: "Confirm" })
  assert.equal((answered.match(/\sdisabled=""/g) ?? []).length, 2)
  assert.match(answered, /aria-pressed="true"/)
  assert.match(answered, /aria-pressed="false"/)
  assert.match(answered, /title="Confirm"/)
})

test("package fields retain visible labels, defaults and their named form values", () => {
  const html = render({
    _type: "Card",
    title: "Delivery",
    confirm: { label: "Save" },
    children: [
      {
        _type: "Form",
        _action: { type: "reply", text: "Save delivery" },
        children: [
          {
            _type: "Input",
            label: "Notes",
            name: "notes",
            defaultValue: "Leave at desk",
          },
          {
            _type: "DatePicker",
            label: "Delivery day",
            name: "day",
            value: "2026-10-20",
          },
        ],
      },
    ],
  })
  assert.equal((html.match(/<form\b/g) ?? []).length, 1)
  assert.match(html, /aria-hidden="true"[^>]*>Notes<\/span>/)
  assert.match(html, /name="notes"/)
  assert.match(html, /value="Leave at desk"/)
  assert.match(html, /data-slot="rich-date-picker"/)
  assert.match(html, /type="hidden" name="day" value="2026-10-20"/)
  assert.match(html, /Save/)
})

test("DatePicker keeps native time and datetime inputs with visible labels", () => {
  for (const inputType of ["time", "datetime-local"]) {
    const html = render({
      _type: "DatePicker",
      inputType,
      label: "Appointment",
      name: "at",
    })
    assert.match(html, new RegExp(`type="${inputType}"`))
    assert.match(html, /aria-hidden="true"[^>]*>Appointment<\/span>/)
    assert.match(html, /name="at"/)
    assert.doesNotMatch(html, /data-slot="rich-date-picker"/)
  }
})

test("markdown renders through the custom renderer and empty media stay empty", () => {
  const html = render({
    _type: "Markdown",
    value: "**Ready**\n\n- First\n- Second",
  })
  assert.match(html, /data-aui="markdown"/)
  assert.match(html, /<strong>Ready<\/strong>/)
  assert.match(html, /<li>Second<\/li>/)
  assert.equal(render({ _type: "Map", places: [] }), "")
  assert.equal(render({ _type: "Diagram", code: " " }), "")
})

test("a team component's drawn tree renders with the base components alone", () => {
  // forge_agent_runtime.generative_ui's drawing of the design brief's example.
  const html = render({
    _type: "Card",
    title: "Order A-1042",
    children: [
      { _type: "Badge", value: "Shipped" },
      { _type: "Fact", label: "Expected delivery", value: "October 22" },
      {
        _type: "ListView",
        children: [{ _type: "ListViewItem", children: ["2 × Desk lamp"] }],
      },
      {
        _type: "Button",
        label: "Track delivery",
        _action: { type: "reply", text: "Track order A-1042" },
      },
    ],
  })
  for (const shown of [
    "Order A-1042",
    "Shipped",
    "October 22",
    "2 × Desk lamp",
    "Track delivery",
  ])
    assert.ok(html.includes(shown), shown)
})
