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
const {
  workflowChangesOf,
  agentChangesOf,
  announceWorkflowChange,
  announceAgentChange,
  changeSummary,
} = await server.ssrLoadModule("/src/features/assistant/lib/builder-changes.ts")
await server.close()

const ORG = "0b8f3a52-9c1e-4d7a-8f30-6a1c2b3d4e01"
const answered = (name, response, key = "functionResponse") => ({
  author: "workflows",
  content: { role: "user", parts: [{ [key]: { id: "c1", name, response } }] },
})
const saved = {
  status: "success",
  payload: {
    workflow_id: "ag_test000001",
    organization_id: ORG,
    revision: 7,
    done: ["Added Look up the order (fetch_order)", 3],
    added: ["fetch_order"],
    problems: [],
  },
}

test("an edit the assistant saved is a change to its workflow's draft", () => {
  assert.deepEqual(workflowChangesOf(answered("edit_workflow", saved)), [
    {
      organizationId: ORG,
      workflowId: "ag_test000001",
      revision: 7,
      done: ["Added Look up the order (fetch_order)"],
    },
  ])
  // The server's own spelling too.
  assert.equal(
    workflowChangesOf(answered("edit_workflow", saved, "function_response"))
      .length,
    1
  )
})

test("anything else changes nothing: a failed edit, another tool, a message", () => {
  const failed = {
    status: "failed",
    reason: "Change 1 (remove_node): ...",
    suggested_fixes: [],
  }
  assert.deepEqual(workflowChangesOf(answered("edit_workflow", failed)), [])
  assert.deepEqual(workflowChangesOf(answered("get_workflow", saved)), [])
  assert.deepEqual(
    workflowChangesOf({
      content: { role: "model", parts: [{ text: "Done." }] },
    }),
    []
  )
  assert.deepEqual(workflowChangesOf({}), [])
  const partial = {
    status: "success",
    payload: { workflow_id: "ag_x", revision: "7" },
  }
  assert.deepEqual(workflowChangesOf(answered("edit_workflow", partial)), [])
})

test("announcing a change works without a browser's BroadcastChannel", () => {
  // Node has one; announcing just mustn't throw where a page's listeners are none.
  announceWorkflowChange({
    organizationId: ORG,
    workflowId: "ag_x",
    revision: 1,
    done: [],
  })
})

const agentSaved = {
  status: "success",
  payload: {
    agent_id: "ca_support01",
    organization_id: ORG,
    revision: 3,
    done: ["Added Search HR policies (knowledge_1)"],
    added: ["knowledge_1"],
    problems: [],
  },
}

test("an agent edit, or an instruction it wrote, is a change to the agent's draft", () => {
  const change = {
    organizationId: ORG,
    agentId: "ca_support01",
    revision: 3,
    done: ["Added Search HR policies (knowledge_1)"],
  }
  assert.deepEqual(agentChangesOf(answered("edit_agent", agentSaved)), [change])
  assert.deepEqual(
    agentChangesOf(
      answered("write_instruction", agentSaved, "function_response")
    ),
    [change]
  )
  // Neither is a workflow's, and a workflow's edit isn't an agent's.
  assert.deepEqual(workflowChangesOf(answered("edit_agent", agentSaved)), [])
  assert.deepEqual(agentChangesOf(answered("edit_workflow", saved)), [])
})

test("an agent tool that saved nothing changes nothing", () => {
  assert.deepEqual(agentChangesOf(answered("get_agent", agentSaved)), [])
  assert.deepEqual(
    agentChangesOf(
      answered("edit_agent", {
        status: "failed",
        reason: "x",
        suggested_fixes: [],
      })
    ),
    []
  )
  // A test conversation saves no draft.
  assert.deepEqual(agentChangesOf(answered("test_agent", agentSaved)), [])
})

test("announcing an agent change works without a page listening", () => {
  announceAgentChange({
    organizationId: ORG,
    agentId: "ca_x",
    revision: 1,
    done: [],
  })
})

test("a change's summary for the person leaves the node IDs out", () => {
  assert.equal(
    changeSummary({
      done: [
        "Updated Billing desk (bill_sub): renamed it 'Billing desk'",
        "Connected: Support (agent) uses as a tool Search (knowledge_1)",
      ],
    }),
    "Updated Billing desk: renamed it 'Billing desk'. Connected: Support uses as a tool Search"
  )
  assert.equal(changeSummary({ done: [] }), undefined)
})
