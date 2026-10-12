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
const { answerFrom, askForm, describeAnswer } = await server.ssrLoadModule(
  "/src/components/forge/assistant/ask-person.ts"
)
await server.close()

/** The form's data, as FormData reads it. */
function filled(values) {
  const all = (name) => {
    const value = values[name]
    return value === undefined ? [] : Array.isArray(value) ? value : [value]
  }
  return { get: (name) => all(name)[0] ?? null, getAll: all }
}

const DEPLOY = {
  type: "object",
  properties: {
    environment: {
      type: "string",
      enum: ["staging", "production"],
      description: "Where it goes",
    },
    replicas: { type: "integer", minimum: 1, maximum: 10, default: 2 },
    notify: { type: "boolean", title: "Tell the team?" },
    regions: {
      type: "array",
      items: { type: "string", enum: ["eu", "us"] },
    },
    owner_email: { type: "string", format: "email" },
  },
  required: ["environment", "replicas"],
}

test("an object asks a question per field, the required ones required", () => {
  const form = askForm(DEPLOY, "How should I deploy main?")
  assert.equal(form.fields, true)
  assert.deepEqual(
    form.questions.map((q) => [q.name, q.title, q.kind, q.required]),
    [
      ["environment", "Environment", "single", true],
      ["replicas", "Replicas", "input", true],
      ["notify", "Tell the team?", "single", false],
      ["regions", "Regions", "multiple", false],
      ["owner_email", "Owner email", "input", false],
    ]
  )
  const [environment, replicas, notify, , email] = form.questions
  assert.equal(environment.description, "Where it goes")
  assert.deepEqual(
    environment.choices?.map((c) => c.value),
    ["staging", "production"]
  )
  assert.deepEqual(replicas.input, {
    type: "number",
    min: 1,
    max: 10,
    step: 1,
  })
  assert.deepEqual(replicas.defaults, ["2"])
  assert.deepEqual(
    notify.choices?.map((c) => c.label),
    ["Yes", "No"]
  )
  assert.equal(email.input?.type, "email")
})

test("anything else is one question, the message its title", () => {
  for (const schema of [undefined, null, { type: "string" }, "nonsense"]) {
    const form = askForm(schema, "What's the ticket number?")
    assert.equal(form.fields, false)
    const [only] = form.questions
    assert.equal(only.title, "What's the ticket number?")
    assert.equal(only.kind, "input")
    assert.equal(only.required, true)
  }
  assert.equal(
    askForm({ type: "boolean" }, "Go ahead?").questions[0].kind,
    "single"
  )
  assert.equal(
    askForm({ type: "string", format: "date" }, "When?").questions[0].input
      ?.type,
    "date"
  )
})

test("answers come back in the schema's types, empty optional fields left out", () => {
  const form = askForm(DEPLOY, "How should I deploy main?")
  assert.deepEqual(
    answerFrom(
      form,
      filled({
        environment: "production",
        replicas: "3",
        notify: "false",
        regions: ["eu", "us"],
        owner_email: "",
      })
    ),
    {
      environment: "production",
      replicas: 3,
      notify: false,
      regions: ["eu", "us"],
    }
  )
})

test("a choice's value is the schema's, whatever its type", () => {
  const form = askForm({ type: "integer", enum: [1, 2, 3] }, "How many?")
  assert.equal(answerFrom(form, filled({ answer: "2" })), 2)
  const yes = askForm({ type: "boolean" }, "Go ahead?")
  assert.equal(answerFrom(yes, filled({ answer: "true" })), true)
})

test("lists split on commas, nested objects read as JSON", () => {
  const form = askForm(
    {
      type: "object",
      properties: {
        tags: { type: "array", items: { type: "string" } },
        limits: { type: "object", properties: { cpu: { type: "number" } } },
      },
    },
    "Settings?"
  )
  assert.deepEqual(
    answerFrom(form, filled({ tags: "a, b,, c", limits: '{"cpu": 2}' })),
    { tags: ["a", "b", "c"], limits: { cpu: 2 } }
  )
})

test("an answer reads as a line", () => {
  const form = askForm(DEPLOY, "How should I deploy main?")
  assert.equal(
    describeAnswer(
      { environment: "staging", notify: true, regions: ["eu", "us"] },
      form
    ),
    "Environment: staging · Tell the team? Yes · Regions: eu, us"
  )
  assert.equal(describeAnswer(false, askForm({ type: "boolean" }, "?")), "No")
  assert.equal(describeAnswer({}, form), "Nothing filled in")
})
