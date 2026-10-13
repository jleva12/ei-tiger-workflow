import assert from "node:assert/strict"
import { fileURLToPath } from "node:url"
import { test } from "node:test"
import { createServer } from "vite"

// A workflow's Bash step: a command run as it's written, in a folder of
// the run's own, reading the run's data from its environment variables.

const server = await createServer({
  configFile: false,
  root: fileURLToPath(new URL("..", import.meta.url)),
  server: { middlewareMode: true, watch: null },
  optimizeDeps: { noDiscovery: true, include: [] },
  resolve: {
    alias: { "@": fileURLToPath(new URL("../src", import.meta.url)) },
  },
})
const load = (path) => server.ssrLoadModule(path)
const { toDocument, toGraph, parseAgent } = await load(
  "/src/features/adk-workflows/lib/document.ts"
)
const model = await load("/src/features/adk-workflows/lib/model.ts")
const { validateAgent } = await load(
  "/src/features/adk-workflows/lib/validate.ts"
)
const { agentTypes } = await load("/src/features/adk-workflows/lib/scope.ts")
const { resolveExpression } = await load(
  "/src/features/steps/lib/expressions.ts"
)
const { expressionSetting, settingScope } = await load(
  "/src/features/steps/lib/fields.ts"
)
const { leavesRunFolder } = await load("/src/features/steps/lib/validate.ts")
const { typeLabel } = await load("/src/features/steps/lib/types.ts")
const { AGENT_JSON_SCHEMA } = await load(
  "/src/features/adk-workflows/lib/schema.ts"
)
await server.close()

const ORG = "org-1"

/** A graph document from nodes `[id, kind, name, config]` and edges `[source, output, target]`. */
function agent(nodes, edges = []) {
  const steps = nodes.map(([id, kind, name, config = {}]) => {
    const base = model.newNode(kind, [], name)
    return {
      id,
      data: { ...base, config: { ...base.config, ...config } },
      position: { x: 0, y: 0 },
    }
  })
  return toDocument(
    {
      id: "ag_test",
      name: "Test",
      description: "",
      organization_id: ORG,
      created_at: "",
      updated_at: "",
    },
    {
      steps,
      connections: edges.map(([source, output, target]) => ({
        source,
        output,
        target,
      })),
    }
  )
}

const variable = (id, name, value) => ({ id, name, value })

/** A start (allowing a file system unless said), a Bash step, and an End after each of its ways. */
function cloning(config = {}, { fileSystem = true } = {}) {
  return agent(
    [
      ["start", "start", "Start", { allow_file_system: fileSystem }],
      [
        "clone",
        "bash",
        "Clone",
        {
          command: 'gh repo clone "$REPO" backend',
          env: [variable("v1", "REPO", "{{ input.repo }}")],
          ...config,
        },
      ],
      ["done", "end", "Done", { result: "steps.clone.output.stdout" }],
      ["failed", "end", "Failed", { outcome: "failed" }],
    ],
    [
      ["start", "next", "clone"],
      ["clone", "success", "done"],
      ["clone", "error", "failed"],
    ]
  )
}

const issuesOf = (doc) => validateAgent(toGraph(doc)).map((i) => i.id)

test("a Bash step is an action with a Success and an Error way, and goes to a document and back", () => {
  assert.equal(model.AGENT_KINDS.bash.group, "actions")
  assert.deepEqual(model.AGENT_KINDS.bash.defaults(), {
    command: "",
    cwd: "",
    env: [],
    timeout_seconds: 300,
  })
  const doc = cloning({ cwd: "repos" })
  assert.deepEqual(doc.nodes[1].outputs, ["success", "error"])
  assert.deepEqual(toDocument({ ...doc }, toGraph(doc)), doc)
  const read = parseAgent(JSON.stringify(doc), { organizationId: ORG })
  assert.deepEqual(read.notes, [])
  assert.deepEqual(read.doc.nodes, doc.nodes)
  // Its variables are rows, each with an ID of its own.
  const odd = structuredClone(doc)
  odd.nodes[1].config.env = [{ name: "A", value: "1" }, "nope"]
  const fixed = parseAgent(JSON.stringify(odd), { organizationId: ORG })
  assert.equal(fixed.doc.nodes[1].config.env.length, 1)
  assert.match(fixed.doc.nodes[1].config.env[0].id, /^env_/)
  assert.deepEqual(fixed.notes, [
    "nodes[1].config.env[1] isn't an object; dropped.",
  ])
})

test("the JSON Schema knows a Bash step's settings", () => {
  const config = AGENT_JSON_SCHEMA.$defs.config_bash
  assert.deepEqual(
    Object.keys(config.properties).sort(),
    Object.keys(model.AGENT_KINDS.bash.defaults()).sort()
  )
  assert.match(
    config.properties.command.description,
    /never filled in with \{\{ \}\}/
  )
})

test("a Bash step needs a start that gives runs a folder, as a Pi agent does", () => {
  assert.deepEqual(issuesOf(cloning()), [])
  assert.deepEqual(issuesOf(cloning({}, { fileSystem: false })), [
    "clone:file-system",
  ])
  const issue = validateAgent(toGraph(cloning({}, { fileSystem: false })))[0]
  assert.equal(
    issue.message,
    "A Bash step runs its command in the run's folder: turn on Allow file system on the start."
  )
})

test("a Bash step's own settings are checked as the worker would refuse them", () => {
  const found = (config) => issuesOf(cloning(config))
  assert.deepEqual(found({ command: "  " }), ["clone:command"])
  assert.deepEqual(found({ timeout_seconds: 0 }), ["clone:timeout"])
  for (const env of [
    [variable("a", "", "x")],
    [variable("a", "MY VAR", "x")],
    [variable("a", "1ST", "x")],
    [variable("a", "WORKSPACE", "x")],
    [variable("a", "REPO", "x"), variable("b", "REPO", "y")],
  ])
    assert.deepEqual(found({ env }), ["clone:env"], JSON.stringify(env))
  // The run's data written into the command isn't filled in: a warning.
  const filled = validateAgent(
    toGraph(cloning({ command: "gh repo clone {{ input.repo }}" }))
  )
  assert.deepEqual(
    filled.map((i) => [i.id, i.level, i.field]),
    [["clone:command-filled", "warning", "command"]]
  )
  // gh's own Go templates are bash's text, as they should be.
  assert.deepEqual(
    found({
      command:
        "gh pr list --json title --template '{{range .}}{{.title}}{{end}}'",
    }),
    []
  )
})

test("a Bash step's folder stays in the run's folder", () => {
  for (const cwd of [
    "",
    "backend",
    "repos/app",
    "./a/../b",
    "{{ state.workspace }}/x",
    "{{ input.dir }}",
  ])
    assert.equal(leavesRunFolder(cwd), false, cwd)
  for (const cwd of ["..", "../x", "a/../../b", "/tmp", "~/code"])
    assert.equal(leavesRunFolder(cwd), true, cwd)
  assert.deepEqual(issuesOf(cloning({ cwd: "../elsewhere" })), ["clone:cwd"])
})

test("its folder and variables read the run's data, and later steps read what it hands on", () => {
  const doc = cloning({
    cwd: "{{ state.workspace }}/{{ input.missing }}",
    env: [
      variable("v1", "REPO", "{{ input.repo }}"),
      variable("v2", "TOKEN", "{{ steps.nope.output }}"),
    ],
  })
  const graph = toGraph(doc)
  const { scopeOf } = agentTypes(graph)
  const step = graph.steps.find((s) => s.id === "clone").data
  for (const key of ["cwd", "env.v1", "env.v2"])
    assert.equal(expressionSetting(step, key).mode, "template", key)
  assert.equal(expressionSetting(step, "command"), undefined)
  const scope = settingScope(expressionSetting(step, "cwd"), scopeOf("clone"))
  assert.equal(typeLabel(resolveExpression("state.workspace", scope)), "string")
  // The workflow's checks find the step that isn't there in a variable's value.
  const ids = issuesOf(doc)
  assert.ok(
    ids.some((id) => id.startsWith("clone:field:env.v2:")),
    ids.join(", ")
  )

  const after = scopeOf("done")
  const type = (expression) => typeLabel(resolveExpression(expression, after))
  assert.equal(type("steps.clone.output.stdout"), "string")
  assert.equal(type("steps.clone.output.exit_code"), "integer")
  assert.equal(type("steps.clone.output.cwd"), "string")
  assert.equal(type("steps.clone.output.truncated"), "boolean")
  const failedScope = scopeOf("failed")
  assert.equal(
    typeLabel(resolveExpression("steps.clone.error.stderr", failedScope)),
    "string"
  )
  assert.equal(
    typeLabel(resolveExpression("steps.clone.error.exit_code", failedScope)),
    "integer"
  )
})
