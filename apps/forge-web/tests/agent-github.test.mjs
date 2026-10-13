import assert from "node:assert/strict"
import { fileURLToPath } from "node:url"
import { test } from "node:test"
import { createServer } from "vite"
import { installNodeCatalog } from "./node-catalog.mjs"

// A workflow's GitHub steps: Clone repository, Create branch and Commit and
// push work with git in the run's folder; Open pull request asks GitHub.

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
const { expressionSetting } = await load("/src/features/steps/lib/fields.ts")
const { typeLabel } = await load("/src/features/steps/lib/types.ts")
const { stepDetail, stepSummary } = await load(
  "/src/features/adk-workflows/components/step-lines.ts"
)
const { agentJsonSchema } = await load(
  "/src/features/adk-workflows/lib/schema.ts"
)
const { kindInfo } = await installNodeCatalog(server)
await server.close()

const ORG = "org-1"
const GITHUB = ["git_clone", "git_branch", "git_push", "github_pull_request"]

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

/** Clone, branch, push and open a pull request, each set as said; an End after each Success. */
function change(configs = {}, { fileSystem = true } = {}) {
  return agent(
    [
      ["start", "start", "Start", { allow_file_system: fileSystem }],
      [
        "clone",
        "git_clone",
        "Clone",
        { repository: "acme/backend", credential: "cred_gh", ...configs.clone },
      ],
      [
        "branch",
        "git_branch",
        "Branch",
        {
          folder: "{{ steps.clone.output.folder }}",
          branch: "forge/{{ input.ticket }}",
          ...configs.branch,
        },
      ],
      [
        "push",
        "git_push",
        "Push",
        {
          folder: "{{ steps.clone.output.folder }}",
          message: "Fix {{ input.ticket }}",
          credential: "cred_gh",
          ...configs.push,
        },
      ],
      [
        "pr",
        "github_pull_request",
        "Pull request",
        {
          repository: "{{ steps.push.output.repository }}",
          head: "{{ steps.push.output.branch }}",
          title: "Fix {{ input.ticket }}",
          credential: "cred_gh",
          ...configs.pr,
        },
      ],
      ["done", "end", "Done", { result: "steps.pr.output.url" }],
      ["failed", "end", "Failed", { outcome: "failed" }],
    ],
    [
      ["start", "next", "clone"],
      ["clone", "success", "branch"],
      ["branch", "success", "push"],
      ["push", "success", "pr"],
      ["pr", "success", "done"],
      ["pr", "error", "failed"],
    ]
  )
}

const credentials = new Map([
  [
    "cred_gh",
    {
      name: "GitHub",
      urls: ["https://github.com", "https://api.github.com"],
      connected: true,
    },
  ],
])
const issuesOf = (doc, context = { credentials }) =>
  validateAgent(toGraph(doc), context).map((i) => i.id)

test("the GitHub steps are served in a GitHub group, each with a Success and an Error way", () => {
  for (const kind of GITHUB) {
    assert.equal(kindInfo(kind).group, "github", kind)
    assert.ok(kindInfo(kind).icon, kind)
    const node = model.newNode(kind)
    assert.deepEqual(
      model.outputsOf(node).map((o) => o.id),
      ["success", "error"],
      kind
    )
  }
})

test("a change from clone to pull request goes to a document and back, and is ready to run", () => {
  const doc = change()
  assert.deepEqual(toDocument({ ...doc }, toGraph(doc)), doc)
  const read = parseAgent(JSON.stringify(doc), { organizationId: ORG })
  assert.deepEqual(read.notes, [])
  assert.deepEqual(issuesOf(doc), [])
})

test("each git step needs a start that gives runs a folder; a pull request doesn't", () => {
  assert.deepEqual(issuesOf(change({}, { fileSystem: false })), [
    "clone:file-system",
    "branch:file-system",
    "push:file-system",
  ])
})

test("their settings are checked as the worker would refuse them", () => {
  const found = (configs) => issuesOf(change(configs))
  assert.deepEqual(found({ clone: { repository: " " } }), ["clone:repository"])
  assert.deepEqual(
    found({ clone: { repository: "git@github.com:acme/app" } }),
    ["clone:repository"]
  )
  assert.deepEqual(found({ clone: { folder: "../elsewhere" } }), [
    "clone:folder",
  ])
  assert.deepEqual(found({ branch: { branch: "two words" } }), [
    "branch:branch",
  ])
  assert.deepEqual(found({ branch: { branch: "-f" } }), ["branch:branch"])
  assert.deepEqual(found({ push: { message: "" } }), ["push:message"])
  assert.deepEqual(found({ pr: { title: "" } }), ["pr:title"])
  assert.deepEqual(found({ pr: { head: "" } }), ["pr:head"])
  // A pull request needs someone signed in; a push without one is a warning.
  assert.deepEqual(found({ pr: { credential: "" } }), ["pr:credential"])
  const push = validateAgent(toGraph(change({ push: { credential: "" } })), {
    credentials,
  })
  assert.deepEqual(
    push.map((i) => [i.id, i.level, i.field]),
    [["push:credential", "warning", "credential"]]
  )
  // A clone of a public repository needs none.
  assert.deepEqual(found({ clone: { credential: "" } }), [])
})

test("a credential not sent to GitHub is caught where the repository is written out", () => {
  const elsewhere = new Map([
    [
      "cred_gh",
      { name: "Orders", urls: ["https://api.example.com"], connected: true },
    ],
  ])
  const ids = validateAgent(
    toGraph(change({ pr: { repository: "acme/backend" } })),
    {
      credentials: elsewhere,
    }
  ).map((i) => i.id)
  assert.ok(ids.includes("clone:credential-url"), ids.join(", "))
  assert.ok(ids.includes("pr:credential-url"), ids.join(", "))
  // Where the repository is a template, it's only known as it runs.
  assert.ok(!ids.includes("push:credential-url"), ids.join(", "))
})

test("their settings read the run's data, and later steps read what each hands on", () => {
  const doc = change()
  const graph = toGraph(doc)
  const { scopeOf } = agentTypes(graph)
  const settings = {
    clone: ["repository", "ref", "folder"],
    branch: ["folder", "branch", "start"],
    push: ["folder", "message", "author_name", "author_email"],
    pr: ["repository", "head", "base", "title", "body"],
  }
  for (const [id, keys] of Object.entries(settings)) {
    const step = graph.steps.find((s) => s.id === id).data
    for (const key of keys)
      assert.equal(
        expressionSetting(step, key).mode,
        "template",
        `${id}.${key}`
      )
  }
  const type = (expression, at = "done") =>
    typeLabel(resolveExpression(expression, scopeOf(at)))
  assert.equal(type("steps.clone.output.folder"), "string")
  assert.equal(type("steps.clone.output.commit"), "string")
  assert.equal(type("steps.branch.output.created"), "boolean")
  assert.equal(type("steps.push.output.files"), "array<string>")
  assert.equal(type("steps.push.output.committed"), "boolean")
  assert.equal(type("steps.pr.output.number"), "integer")
  assert.equal(type("steps.pr.output.url"), "string")
  assert.equal(type("steps.pr.error.status", "failed"), "integer")
  assert.equal(type("steps.push.error.stderr", "failed"), "string")
})

test("their cards say what each does", () => {
  const graph = toGraph(change({ clone: { ref: "develop", depth: 1 } }))
  const data = (id) => graph.steps.find((s) => s.id === id).data
  assert.equal(
    stepDetail(data("clone")),
    "Into backend · on develop · last 1 commit"
  )
  assert.deepEqual(stepSummary(data("clone")), { code: "acme/backend" })
  assert.equal(stepDetail(data("pr")), "Into the default branch")
  assert.deepEqual(stepSummary(data("push")), {
    text: "Fix {{ input.ticket }}",
  })
})

test("the JSON Schema knows each GitHub step's settings", () => {
  const schema = agentJsonSchema()
  for (const kind of GITHUB) {
    const config = schema.$defs[`config_${kind}`]
    assert.deepEqual(
      Object.keys(config.properties).sort(),
      Object.keys(kindInfo(kind).defaults()).sort(),
      kind
    )
  }
  assert.ok(schema.$defs.node.properties.kind.enum.includes("git_push"))
})
