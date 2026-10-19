import assert from "node:assert/strict"
import { fileURLToPath } from "node:url"
import { test } from "node:test"
import { createServer } from "vite"

const server = await createServer({
  configFile: false,
  root: fileURLToPath(new URL("..", import.meta.url)),
  resolve: {
    alias: { "@": fileURLToPath(new URL("../src", import.meta.url)) },
  },
  server: { middlewareMode: true, watch: null },
  optimizeDeps: { noDiscovery: true, include: [] },
})
const load = (name) => server.ssrLoadModule(`/src/components/forge/generative-ui/${name}.ts`)
const { presentCatalog, schemaCost, withRequired, DEFAULT_RICH_ANSWER_COMPONENTS } = await load("catalog")
const { choicesOf } = await load("choices-options")
const { offersChoices, shownOf } = await load("messages")
const { drawnOf, namesTeamComponents, treeOf, UI_PROTOCOL } = await load("result")
const { replyOf } = await load("actions")
const { withCardForms, withQuietFields } = await load("composition")
await server.close()

/** A reply whose present call is `args`, answered `result`. */
const reply = (args, result = { shown: true }) => ({
  role: "assistant",
  content: [
    { type: "text", text: "Here it is." },
    { type: "tool-call", toolName: "present", args, result },
  ],
})

const NEXT = {
  _type: "Choices",
  options: [
    { label: "By region", reply: "Break it down by region." },
    { label: "Next quarter" },
  ],
}

test("a choice sends its reply, or its label without one", () => {
  assert.deepEqual(choicesOf(NEXT.options), [
    { label: "By region", reply: "Break it down by region." },
    { label: "Next quarter", reply: "Next quarter" },
  ])
  // What the model wrote wrong is left out, not drawn as an empty button.
  assert.deepEqual(
    choicesOf([{ reply: "No label" }, "x", null, { label: " " }]),
    []
  )
  assert.deepEqual(choicesOf(undefined), [])
})

test("a reply offering choices, however deep, hides the follow-up chips", () => {
  const card = {
    _type: "Card",
    children: ["Q1 grew.", { _type: "Row", children: [NEXT] }],
  }
  assert.equal(offersChoices(reply(card)), true)
  assert.equal(
    offersChoices(reply({ _type: "Card", children: ["No choices."] })),
    false
  )
  // One the server refused isn't drawn, so its choices aren't offered,
  // however ADK hands the result over (JSON text, as it does, or a value).
  assert.equal(
    offersChoices(reply(NEXT, { shown: false, problems: [] })),
    false
  )
  assert.equal(
    offersChoices(reply(NEXT, '{"shown": false, "problems": []}')),
    false
  )
  assert.equal(offersChoices(reply(NEXT, '{"shown": true}')), true)
  assert.equal(
    offersChoices({ role: "user", content: [{ type: "text", text: "hi" }] }),
    false
  )
  assert.equal(offersChoices(undefined), false)
})

test("the catalog has every package component and Choices, each with its own props", () => {
  const catalog = presentCatalog()
  const names = catalog.groups.flatMap((group) => group.components)
  assert.equal(names.length, 36)
  assert.deepEqual(Object.keys(catalog.components), names)
  assert.deepEqual(catalog.defaults, [...DEFAULT_RICH_ANSWER_COMPONENTS])
  assert.deepEqual(Object.keys(catalog.components.Fact.properties).sort(), [
    "delta",
    "label",
    "trend",
    "upIsGood",
    "value",
  ])
  assert.equal(
    catalog.components.Choices.properties.options.items.required[0],
    "label"
  )
  // The node's frame isn't any component's: the runtime adds it.
  for (const component of Object.values(catalog.components)) {
    for (const key of ["_type", "_key", "children"]) {
      assert.ok(!(key in component.properties))
    }
  }
  assert.deepEqual(catalog.action.properties.type.enum, ["reply"])
  assert.equal(catalog.children.items.anyOf[1].$ref, "#/$defs/node")
})

test("a pick brings what it needs, in the offered order", () => {
  assert.deepEqual(withRequired(["Carousel"]), ["Card", "Carousel"])
  assert.deepEqual(withRequired(["ListViewItem"]), ["ListView", "ListViewItem"])
  assert.deepEqual(withRequired(["Chart", "Card", "Gauge"]), ["Card", "Chart"])
})

test("more components cost more tokens in every call, more on Gemini", () => {
  const few = schemaCost(["Card", "Fact"])
  const defaults = schemaCost(DEFAULT_RICH_ANSWER_COMPONENTS)
  const all = schemaCost(presentCatalog().groups.flatMap((g) => g.components))
  assert.ok(few.openai < defaults.openai && defaults.openai < all.openai)
  assert.ok(defaults.gemini > defaults.openai * 2)
  // Near what the providers counted (OpenAI ~1,565 and ~4,140; Gemini ~4,425 and ~11,810).
  assert.ok(Math.abs(defaults.openai - 1565) < 400, String(defaults.openai))
  assert.ok(Math.abs(all.gemini - 11810) < 3000, String(all.gemini))
  assert.deepEqual(schemaCost([]), { openai: 0, gemini: 0 })
})

test("what a control does comes back as the person's words and values", () => {
  assert.equal(replyOf({ type: "reply", text: "Approve" }), "Approve")
  assert.equal(
    replyOf({ type: "reply", text: "Region:", $input: "EMEA" }),
    "Region: EMEA"
  )
  assert.equal(
    replyOf({
      type: "reply",
      text: "Book it",
      $input: {
        name: "Ada",
        nights: 3,
        breakfast: true,
        extras: ["gym", "spa"],
        note: "",
      },
    }),
    "Book it\n- name: Ada\n- nights: 3\n- breakfast: Yes\n- extras: gym, spa"
  )
  // Without text: the values alone, or the action's type in words.
  assert.equal(replyOf({ type: "reply", $input: ["a", "b"] }), "a, b")
  assert.equal(replyOf({ type: "show_details" }), "show details")
  assert.equal(replyOf({ type: "reply" }), "")
  assert.equal(replyOf(null), "")
})

test("the server's verdict on a call is read however ADK hands it over", () => {
  assert.equal(shownOf('{"shown": true}'), true)
  assert.equal(shownOf('{"shown": false, "problems": ["x"]}'), false)
  assert.equal(shownOf({ shown: false }), false)
  assert.equal(shownOf({ result: { shown: false } }), false)
  assert.equal(shownOf('{"result": {"shown": true}}'), true)
  // Before it answers, or something else: no verdict.
  assert.equal(shownOf(undefined), undefined)
  assert.equal(shownOf("not json"), undefined)
  assert.equal(shownOf({ ok: true }), undefined)
})

/** A team component's call, and the base tree the server drew it as. */
const TEAM_CALL = {
  _type: "Card",
  children: [
    { _type: "org.shipment-summary", props: { order_number: "A-1042" } },
  ],
}
const TEAM_TREE = {
  _type: "Card",
  children: [
    {
      _type: "Card",
      title: "Order A-1042",
      children: [{ _type: "Badge", value: "Shipped" }],
    },
  ],
}
const drawnAnswer = (tree = TEAM_TREE, protocol = UI_PROTOCOL) => ({
  shown: true,
  note: "The person sees it now.",
  ui: {
    protocol,
    components: [{ key: "org.shipment-summary", version: 3 }],
    tree,
  },
})

test("a team component's call is drawn from the tree the server drew", () => {
  assert.equal(treeOf(TEAM_CALL, drawnAnswer()), TEAM_TREE)
  // However ADK hands the answer over.
  assert.deepEqual(treeOf(TEAM_CALL, JSON.stringify(drawnAnswer())), TEAM_TREE)
  assert.deepEqual(treeOf(TEAM_CALL, { result: drawnAnswer() }), TEAM_TREE)
  assert.deepEqual(drawnOf(drawnAnswer()).components, [
    { key: "org.shipment-summary", version: 3 },
  ])
  // Base components alone: their arguments, as always.
  const plain = { _type: "Card", children: ["Hi"] }
  assert.equal(treeOf(plain, { shown: true }), plain)
  assert.equal(treeOf(plain, undefined), plain)
  // Team components with no tree this chat reads: nothing drawn, never
  // their names drawn as base components.
  assert.equal(treeOf(TEAM_CALL, { shown: true }), undefined)
  assert.equal(treeOf(TEAM_CALL, drawnAnswer(TEAM_TREE, 2)), undefined)
  assert.equal(treeOf({ $type: "org.note" }, undefined), undefined)
  assert.equal(drawnOf({ shown: true, ui: { protocol: 1 } }), undefined)
  assert.equal(namesTeamComponents(TEAM_CALL), true)
  assert.equal(namesTeamComponents({ _type: "Card", title: "org.x" }), false)
})

test("choices a team component drew hide the follow-up chips too", () => {
  const withChoices = {
    _type: "Card",
    children: [{ _type: "Row", children: [NEXT] }],
  }
  assert.equal(offersChoices(reply(TEAM_CALL, drawnAnswer(withChoices))), true)
  assert.equal(offersChoices(reply(TEAM_CALL, drawnAnswer())), false)
  // The call's own arguments name no Choices: what was drawn is read.
  assert.equal(offersChoices(reply({ ...TEAM_CALL, children: [NEXT] }, drawnAnswer())), false)
})

test("a form's fields don't act on their own: its confirm sends them", () => {
  const field = (type, extra = {}) => ({
    _type: type,
    name: type.toLowerCase(),
    _action: { type: "reply", text: `${type}:` },
    ...extra,
  })
  const quiet = withQuietFields({
    _type: "Col",
    children: [
      {
        _type: "Card",
        asForm: true,
        confirm: {
          label: "Send",
          _action: { type: "reply", text: "Details:" },
        },
        children: [
          field("DatePicker"),
          { _type: "Row", children: [field("Select"), field("Slider")] },
          field("Button"),
        ],
      },
      { _type: "Form", _action: { type: "reply" }, children: [field("Input")] },
      // Outside a form a field acts as it says.
      field("Checkbox"),
    ],
  })
  const [card, form, checkbox] = quiet.children
  assert.equal(card.children[0]._action, undefined)
  assert.equal(card.children[1].children[0]._action, undefined)
  assert.equal(card.children[1].children[1]._action, undefined)
  // Buttons, the confirm, the Form itself and fields outside forms keep theirs.
  assert.deepEqual(card.children[2]._action, { type: "reply", text: "Button:" })
  assert.equal(card.confirm._action.text, "Details:")
  assert.deepEqual(form._action, { type: "reply" })
  assert.equal(form.children[0]._action, undefined)
  assert.equal(checkbox._action.text, "Checkbox:")
  // Names stay: they're how the form sends the values.
  assert.equal(card.children[0].name, "datepicker")
})

test("a card's confirm sends the form inside it: the card becomes that form", () => {
  const reply = { type: "reply", text: "Contact details:" }
  // As a model sent it: the button on the card, the action on its form.
  const sent = {
    _type: "Card",
    title: "Start a new claim",
    confirm: { label: "Continue" },
    children: [
      { _type: "Text", value: "Your details." },
      {
        _type: "Form",
        _action: reply,
        children: [
          { _type: "Input", name: "full_name" },
          { _type: "Input", name: "email" },
        ],
      },
      { _type: "Caption", value: "Phone is optional." },
    ],
  }
  const card = withCardForms({ _type: "Col", children: [sent] }).children[0]
  assert.equal(card.asForm, true)
  assert.deepEqual(card._action, reply)
  assert.deepEqual(
    card.children.map((c) => c.name ?? c._type),
    ["Text", "full_name", "email", "Caption"]
  )
  // A confirm that acts, or a card that's a form already, stays as it is.
  const acting = { ...sent, confirm: { label: "Go", _action: reply } }
  assert.deepEqual(withCardForms(acting), acting)
  const own = { ...sent, asForm: true, _action: reply }
  assert.deepEqual(withCardForms(own), own)
  // Without a form that acts inside, there's nothing to send.
  const empty = { ...sent, children: [{ _type: "Text", value: "Hi" }] }
  assert.deepEqual(withCardForms(empty), empty)
})
