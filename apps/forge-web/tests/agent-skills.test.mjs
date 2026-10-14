import assert from "node:assert/strict"
import { fileURLToPath } from "node:url"
import { test } from "node:test"
import { createServer } from "vite"
import { installNodeCatalog } from "./node-catalog.mjs"

// Skills in the builders, without a browser: a skill's checks before it's
// saved to the vault, and what the checks say about an agent's Skills tool
// on the Agents canvas and in a workflow's LLM agent.

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
const form = await load("/src/features/skills/lib/skill-form.ts")
const chat = {
  ...(await load("/src/features/agents/lib/document.ts")),
  ...(await load("/src/features/agents/lib/validate.ts")),
  model: await load("/src/features/agents/lib/model.ts"),
}
const flow = {
  ...(await load("/src/features/adk-workflows/lib/document.ts")),
  ...(await load("/src/features/adk-workflows/lib/validate.ts")),
  model: await load("/src/features/adk-workflows/lib/model.ts"),
  tools: await load("/src/features/adk-workflows/lib/tools.ts"),
}
await installNodeCatalog(server)
await server.close()

const ORG = "org-1"
const skills = new Map([["sk_triage", { name: "triage" }]])

function build(kit, id, nodes, edges = []) {
  const steps = nodes.map(([step, kind, name, config = {}]) => {
    const base = kit.model.newNode(kind, [], name)
    return {
      id: step,
      data: { ...base, config: { ...base.config, ...config } },
      position: { x: 0, y: 0 },
    }
  })
  return kit.toDocument(
    {
      id,
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

test("a skill is checked as the vault takes it", () => {
  const skill = {
    name: "triage",
    description: "Triages tickets. Use when one comes in.",
    instructions: "1. Read it.",
    files: [{ path: "references/tags.md", content: "bug", encoding: "utf-8" }],
  }
  assert.deepEqual(form.skillProblems(skill), {})
  const bad = form.skillProblems({
    ...skill,
    name: "Triage Tickets",
    description: " ",
    instructions: "",
    files: [
      { path: "../secrets", content: "", encoding: "utf-8" },
      { path: "SKILL.md", content: "", encoding: "utf-8" },
      { path: "a.md", content: "", encoding: "utf-8" },
      { path: "a.md", content: "", encoding: "utf-8" },
    ],
  })
  assert.match(bad.name, /joined by single hyphens/)
  assert.ok(bad.description && bad.instructions)
  assert.deepEqual(Object.keys(bad.paths), ["0", "1", "3"])
  assert.equal(form.slugOf("  PDF Forms!! (v2) "), "pdf-forms-v2")
  assert.equal(
    form.fileBytes({ path: "x", content: "AAEC", encoding: "base64" }),
    3
  )
  assert.equal(
    form.sourceLabel({
      kind: "github",
      repository: "https://github.com/acme/skills",
      ref: "main",
      commit: "a",
      path: "skills/pdf",
    }),
    "GitHub · acme/skills/skills/pdf"
  )
})

test("an agent's Skills tool picks skills that are in the vault", () => {
  const doc = build(
    chat,
    "ca_test",
    [
      ["agent", "agent", "Helper", { instruction: "Help." }],
      ["none", "skills", "Skills", { skills: [] }],
      [
        "gone",
        "skills",
        "More skills",
        { skills: ["sk_triage", "sk_deleted"] },
      ],
      ["ok", "skills", "Triage", { skills: ["sk_triage"] }],
    ],
    [
      ["agent", "tools", "none"],
      ["agent", "tools", "gone"],
      ["agent", "tools", "ok"],
    ]
  )
  const found = chat
    .validateChatAgent(chat.toGraph(doc), { skills })
    .filter((i) => i.field === "skills")
  assert.deepEqual(
    found.map((i) => i.id),
    ["none:skills", "gone:skills-gone"]
  )
  // While the vault loads, a pick isn't called deleted.
  assert.deepEqual(
    chat
      .validateChatAgent(chat.toGraph(doc))
      .filter((i) => i.field === "skills")
      .map((i) => i.id),
    ["none:skills"]
  )
})

test("a workflow's LLM agent has one Skills tool", () => {
  const tool = (id, picked) => ({
    ...flow.tools.newTool("skills", []),
    id,
    config: { skills: picked, run_scripts: false },
  })
  const doc = build(
    flow,
    "ag_test",
    [
      ["start", "start", "Start"],
      [
        "a",
        "llm",
        "Helper",
        {
          instruction: "Help.",
          tools: [tool("t1", ["sk_triage"]), tool("t2", ["sk_gone"])],
        },
      ],
    ],
    [["start", "next", "a"]]
  )
  const found = flow
    .validateAgent(flow.toGraph(doc), { skills })
    .filter((i) => i.id.startsWith("a:tool-"))
  assert.deepEqual(
    found.map((i) => [i.id, i.field]),
    [
      ["a:tool-t2-skills-gone", "tools.t2.skills"],
      ["a:tool-t2-skills-twice", "tools.t2.skills"],
    ]
  )
})
