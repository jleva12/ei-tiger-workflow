import { modelLine } from "@/features/adk-workflows/components/agent-lines"
import type { BuilderUi, NodeSummary } from "@/features/builder/components/ui"
import { CHAT_AGENTS_ICON, type ChatStep } from "@/features/agents/lib/model"
import { ChatAgentFields } from "./chat-agent-fields"
import type { ChatLookups } from "./chat-agent-store"

/*
 * What the Agents page shows that the builder kit can't know: its words,
 * each kind's settings (./chat-agent-fields), and the line under a node's
 * name and on its card.
 */

const HAND_OFF: Record<string, string> = {
  chat: "Takes over",
  task: "Does a task",
  single_turn: "Answers once",
}

const hostOf = (url: string) => {
  try {
    return new URL(url).host
  } catch {
    return ""
  }
}

/** What a node shows under its name: one fact about how it's set up. */
function detailOf(step: ChatStep, lookups: ChatLookups): string {
  switch (step.kind) {
    case "agent":
      return modelLine(step.config, lookups)
    case "sub_agent":
      return `${HAND_OFF[step.config.mode]} · ${modelLine(step.config, lookups)}`
    case "saved_agent": {
      const name = lookups.agents[step.config.agent]
      return name ? `Uses ${name}` : "No agent picked"
    }
    case "adk_workflow": {
      const name = lookups.workflows[step.config.workflow]
      return name ? `Runs ${name}` : "No workflow picked"
    }
    case "memory":
      return step.config.mode === "every_turn"
        ? "With every message"
        : "When it decides to"
    case "http_tool":
      return step.config.confirm ? "Asks before calling" : "Calls it directly"
    case "openapi":
      return step.config.source === "url"
        ? hostOf(step.config.url) || "Spec from a URL"
        : "Pasted spec"
    case "mcp": {
      if (!step.config.server)
        return step.config.transport === "sse" ? "SSE" : "Streamable HTTP"
      const server = lookups.mcpServers[step.config.server]
      return server ? server.name : "MCP server deleted"
    }
    case "knowledge_base": {
      const picked = step.config.knowledge_bases
      if (!picked.length) return "No knowledge base picked"
      const names = picked.map(
        (id) => lookups.knowledgeBases[id]?.name ?? "Deleted knowledge base"
      )
      return names.length > 2
        ? `${names[0]} and ${names.length - 1} more`
        : names.join(" and ")
    }
  }
}

/** A node's line on its card: what it does. */
function summaryOf(step: ChatStep): NodeSummary | null {
  switch (step.kind) {
    case "agent":
    case "sub_agent":
      return step.config.instruction.trim()
        ? { text: step.config.instruction.trim() }
        : null
    case "http_tool":
      return {
        tag: step.config.method,
        code: step.config.url.trim() || "No URL yet",
      }
    case "openapi":
      return step.config.operations.trim()
        ? { code: step.config.operations.trim() }
        : { text: "Every operation" }
    case "knowledge_base":
      return step.config.description.trim()
        ? { text: step.config.description.trim() }
        : { text: `Up to ${step.config.max_results} passages a search` }
    case "mcp":
      if (step.config.server)
        return step.config.tools.trim()
          ? { code: step.config.tools.trim() }
          : { text: "Every tool" }
      return { code: step.config.url.trim() || "No URL yet" }
    default:
      return null
  }
}

export const CHAT_AGENT_UI: BuilderUi = {
  nouns: { doc: "agent", docs: "agents", step: "node", steps: "nodes" },
  docIcon: CHAT_AGENTS_ICON,
  listView: "chat-agents",
  permission: "agents:manage",
  detailOf: (step, lookups) =>
    detailOf(step as ChatStep, lookups as ChatLookups),
  summaryOf: (step) => summaryOf(step as ChatStep),
  Fields: ChatAgentFields as BuilderUi["Fields"],
  // Its fields show the name, with what ADK or the model calls it.
  ownsName: true,
  // Tinted as the workflow builder's groups are: agents, and tools as its actions.
  kindTones: { agents: "agent", tools: "action" },
  // The one agent a chat runs: where every conversation starts.
  badgeOf: (step) => (step.kind === "agent" ? "Entry point" : null),
  readOnlyHint: "Published versions never change: start a new version to edit it",
  emptyCanvas:
    "Add the chat agent from the library, then drag tools and agents onto its ways out.",
  ready: "The agent and everything attached to it are set up.",
}
