import assert from "node:assert/strict"
import { fileURLToPath } from "node:url"
import { test } from "node:test"
import { createServer } from "vite"
import { installNodeCatalog } from "./node-catalog.mjs"

// Where the nodes that work in the run's folder work (a clone, the git
// steps after it, a Bash step, a Pi agent), as their folder fields say
// before a run: the path from the run's folder, and the folders earlier
// steps offer.

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
const model = await load("/src/features/adk-workflows/lib/model.ts")
const folders = await load("/src/features/adk-workflows/lib/folders.ts")
const { toDocument, toGraph } = await load(
  "/src/features/adk-workflows/lib/document.ts"
)
const { validateAgent } = await load(
  "/src/features/adk-workflows/lib/validate.ts"
)
const { stepDetail } = await load(
  "/src/features/adk-workflows/components/step-lines.ts"
)
await installNodeCatalog(server)
await server.close()

/** Nodes by ID, from `[id, kind, name, config]`. */
function nodes(...list) {
  return new Map(
    list.map(([id, kind, name, config = {}]) => {
      const base = model.newNode(kind, [], name)
      return [id, { ...base, config: { ...base.config, ...config } }]
    })
  )
}

const text = (place) => place.parts.map((part) => part.text).join("/")

test("a clone goes into a folder named after its repository, or the one it's given", () => {
  const steps = nodes(
    ["a", "git_clone", "Clone", { repository: "acme/backend" }],
    [
      "b",
      "git_clone",
      "Clone 2",
      {
        repository: "https://git.acme.dev/x/web.git",
        folder: "apps/{{ input.name }}",
      },
    ],
    ["c", "git_clone", "Clone 3", { repository: "{{ input.repo }}" }]
  )
  assert.equal(text(folders.folderOf("a", steps)), "backend")
  const b = folders.folderOf("b", steps)
  assert.equal(text(b), "apps/{{ input.name }}")
  assert.deepEqual(
    b.parts.map((part) => Boolean(part.later)),
    [false, true]
  )
  // A repository only known as it runs: its folder's name is too.
  assert.deepEqual(folders.folderOf("c", steps).parts, [
    { text: "the repository's name", later: true },
  ])
})

test("a step's folder can be another's, followed to where that one works", () => {
  const steps = nodes(
    [
      "clone",
      "git_clone",
      "Clone repository",
      { repository: "acme/backend", folder: "repos/backend" },
    ],
    [
      "branch",
      "git_branch",
      "Create branch",
      { folder: "{{ steps.clone.output.folder }}" },
    ],
    [
      "pi",
      "pi_agent",
      "Explore",
      { folder: "{{steps.branch.output.folder}}/src" },
    ],
    ["bash", "bash", "Build", { cwd: "{{ steps.pi.output.folder }}/../.." }],
    ["self", "pi_agent", "Loop", { folder: "{{ steps.self.output.folder }}/x" }]
  )
  const pi = folders.folderOf("pi", steps)
  assert.equal(text(pi), "repos/backend/src")
  assert.deepEqual(pi.from, { id: "branch", name: "Create branch" })
  assert.equal(text(folders.folderOf("bash", steps)), "repos")
  // One that names itself is shown as written, not followed forever.
  assert.equal(
    text(folders.folderOf("self", steps)),
    "{{ steps.self.output.folder }}/x"
  )
})

test("the run's folder, written out or as state.workspace, and paths that leave it", () => {
  const place = (value) => folders.placeOf(value, undefined, new Map())
  assert.equal(folders.placeLabel(place("")), "the run's folder")
  assert.equal(text(place("{{ state.workspace }}/x/./y")), "x/y")
  assert.equal(text(place("a/b/../c")), "a/c")
  for (const outside of ["..", "../x", "a/../../b", "/tmp", "~/code"])
    assert.equal(place(outside).outside, true, outside)
  assert.equal(place("a/{{ input.x }}/..").outside, undefined)
})

test("a later node is offered the folders earlier steps work in", () => {
  const steps = nodes(
    ["clone", "git_clone", "Clone repository", { repository: "acme/backend" }],
    ["http", "http", "Look up"],
    ["build", "bash", "Build", { cwd: "out" }]
  )
  const choices = folders.folderChoices(["clone", "http", "build"], steps)
  assert.deepEqual(
    choices.map((c) => [c.id, c.value, folders.placeLabel(c.place)]),
    [
      ["clone", "{{ steps.clone.output.folder }}", "backend"],
      ["build", "{{ steps.build.output.cwd }}", "out"],
    ]
  )
})

/** A start that allows a file system, a clone, and a git step after it set as said. */
function after(kind, config) {
  const steps = [
    ["start", "start", "Start", { allow_file_system: true }],
    ["clone", "git_clone", "Clone repository", { repository: "acme/backend" }],
    ["next", kind, "Next", config],
  ]
  return toDocument(
    {
      id: "ag_test",
      name: "Test",
      description: "",
      organization_id: "org-1",
      created_at: "",
      updated_at: "",
    },
    {
      steps: steps.map(([id, k, name, c]) => {
        const base = model.newNode(k, [], name)
        return {
          id,
          data: { ...base, config: { ...base.config, ...c } },
          position: { x: 0, y: 0 },
        }
      }),
      connections: [
        { source: "start", output: "next", target: "clone" },
        { source: "clone", output: "success", target: "next" },
      ],
    }
  )
}

test("a git step left in the run's folder after a clone into its own is warned of", () => {
  const issues = (doc) =>
    validateAgent(toGraph(doc)).filter((i) => i.id === "next:folder-repository")
  const [warned] = issues(after("git_branch", { branch: "x" }))
  assert.equal(warned.level, "warning")
  assert.equal(warned.field, "folder")
  assert.equal(
    warned.message,
    "It works in the run's folder, but Clone repository clones into backend: use its folder."
  )
  assert.deepEqual(
    issues(
      after("git_push", {
        message: "m",
        folder: "{{ steps.clone.output.folder }}",
      })
    ),
    []
  )
})

test("a clone's card says where it clones", () => {
  const steps = nodes([
    "a",
    "git_clone",
    "Clone",
    { repository: "acme/backend", ref: "develop" },
  ])
  assert.equal(stepDetail(steps.get("a")), "Into backend · on develop")
})
