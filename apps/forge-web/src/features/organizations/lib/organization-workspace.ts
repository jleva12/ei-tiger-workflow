import {
  AiBrain01Icon,
  BookOpen01Icon,
  FlowSquareIcon,
  Key01Icon,
  McpServerIcon,
} from "@hugeicons/core-free-icons"

import type { IconProp } from "@/components/forge/icons"
import type { OverviewPeriod } from "@/features/overview/lib/overview"
import type { AdkRunTab } from "@/features/runs/lib/runs"

/**
 * The pages of an organization's workspace
 * (`/organizations/$organizationId?view=…`), as its sub nav lists them:
 * Overview (what its workflows, agents and assistant did and used, and
 * what it cost, over 7, 30 or 90 days: `period`), Workflows (its Google ADK graph workflows, each opening its builder
 * at /organizations/$organizationId/agents/$agentId: `agents` in the URL
 * and the API, from before they were called that), Agents (its chat
 * agents, each opening its builder at
 * /organizations/$organizationId/chat-agents/$chatAgentId), MCP servers
 * (the remote MCP servers its agents use as tools, each opening in a dialog),
 * Members (who holds which role in it; its admins change them) and API keys
 * (what outside apps send to call its agents and workflows). Overview is
 * the default, so it has no `view` in the URL.
 */
export const WORKSPACE_VIEWS = {
  // What the organization's workflows, agents and assistant did and used.
  overview: { label: "Overview", icon: "dashboard" },
  agents: { label: "Workflows", icon: FlowSquareIcon },
  "chat-agents": { label: "Agents", icon: AiBrain01Icon },
  "mcp-servers": { label: "MCP servers", icon: McpServerIcon },
  // Each knowledge base opens its own page, at
  // /organizations/$organizationId/knowledge/$knowledgeBaseId.
  knowledge: { label: "Knowledge bases", icon: BookOpen01Icon },
  config: { label: "Members", icon: "team" },
  "api-keys": { label: "API keys", icon: Key01Icon },
} as const satisfies Record<string, { label: string; icon: IconProp }>

export type WorkspaceView = keyof typeof WORKSPACE_VIEWS

/**
 * How the sub nav groups the pages under Overview, each section under its
 * heading: what the organization builds, what its agents connect to, and
 * its settings.
 */
export const WORKSPACE_SECTIONS: { title: string; views: WorkspaceView[] }[] = [
  { title: "Workflows and agents", views: ["agents", "chat-agents"] },
  { title: "Integrations", views: ["mcp-servers", "knowledge"] },
  { title: "Settings", views: ["config", "api-keys"] },
]

/** The page the workspace opens on, which its URL leaves out. */
export const DEFAULT_VIEW: WorkspaceView = "overview"

export const isWorkspaceView = (value: unknown): value is WorkspaceView =>
  typeof value === "string" && Object.hasOwn(WORKSPACE_VIEWS, value)

/**
 * What the workspace's URL says beyond the organization: the page (`view`)
 * and what's open on it. The route reads it (`validateSearch`), and links
 * into the workspace are made of it.
 */
export type WorkspaceSearch = {
  /** The page; the overview when absent. */
  view?: WorkspaceView
  /** The overview's period; 7 days when absent. */
  period?: OverviewPeriod
  /** Workflows' tab: Runs; Overview when absent. */
  agentsTab?: "runs"
  /** On workflows, a run to open (its ID). */
  agentRun?: string
  /** The run's page's tab; Overview when absent. */
  agentRunTab?: Exclude<AdkRunTab, "overview">
  /** On MCP servers, the server open in its dialog (its ID). */
  mcpServer?: string
}
