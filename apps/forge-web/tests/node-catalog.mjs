import { readFile } from "node:fs/promises"

// The kinds of node as the admin API serves them: a workflow's (GET
// /workflow-node-kinds, the workflows task's node catalog) and the Agents
// canvas's (GET /chat-agent-node-kinds, the agent runtime's), read from
// their files, as the builders install them once they're loaded.
const WORKFLOW = new URL(
  "../../../packages/python/adk-workflows/src/forge_task_adk_workflows/graph/node_kinds.json",
  import.meta.url
)
const CHAT = new URL(
  "../../../packages/python/agent-runtime/src/forge_agent_runtime/node_kinds.json",
  import.meta.url
)

export const readNodeCatalogs = async () => ({
  workflow: JSON.parse(await readFile(WORKFLOW, "utf8")),
  chat: JSON.parse(await readFile(CHAT, "utf8")),
})

/**
 * Installs both node catalogs in `server`'s modules; answers the workflow
 * catalog module, with the chat one's exports beside it.
 */
export async function installNodeCatalog(server) {
  const served = await readNodeCatalogs()
  const workflow = await server.ssrLoadModule(
    "/src/features/adk-workflows/lib/catalog.ts"
  )
  const chat = await server.ssrLoadModule("/src/features/agents/lib/catalog.ts")
  workflow.installNodeCatalog(served.workflow)
  chat.installChatNodeCatalog(served.chat)
  return { ...chat, ...workflow }
}
