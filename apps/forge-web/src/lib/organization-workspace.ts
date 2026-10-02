import { AiNetworkIcon, ChatBotIcon } from "@hugeicons/core-free-icons"

import type { IconProp } from "@/components/forge/icons"
import type { AdkRunTab } from "@/lib/agents/runs"

/**
 * The pages of an organization's workspace
 * (`/organizations/$organizationId?view=…`), as its sub nav lists them:
 * ADK workflows (its Google ADK graph workflows, each opening its builder
 * at /organizations/$organizationId/agents/$agentId: `agents` in the URL
 * and the API, from before they were called that), Agents (its chat
 * agents, each opening its builder at
 * /organizations/$organizationId/chat-agents/$chatAgentId) and Members (who
 * holds which role in it; its admins change them). ADK workflows is the
 * default, so it has no `view` in the URL.
 */
export const WORKSPACE_VIEWS = {
  agents: { label: "ADK workflows", icon: AiNetworkIcon },
  // The sub nav draws the assistant's mark for it, as its launcher wears.
  "chat-agents": { label: "Agents", icon: ChatBotIcon, mark: "assistant" },
  config: { label: "Members", icon: "team" },
} as const satisfies Record<
  string,
  { label: string; icon: IconProp; mark?: "assistant" }
>

export type WorkspaceView = keyof typeof WORKSPACE_VIEWS

/** The page the workspace opens on, which its URL leaves out. */
export const DEFAULT_VIEW: WorkspaceView = "agents"

export const isWorkspaceView = (value: unknown): value is WorkspaceView =>
  typeof value === "string" && Object.hasOwn(WORKSPACE_VIEWS, value)

/**
 * What the workspace's URL says beyond the organization: the page (`view`)
 * and what's open on it. The route reads it (`validateSearch`), and links
 * into the workspace are made of it.
 */
export type WorkspaceSearch = {
  /** The page; ADK workflows when absent. */
  view?: WorkspaceView
  /** ADK workflows' tab: Runs; Overview when absent. */
  agentsTab?: "runs"
  /** On ADK workflows, a run to open (its ID). */
  agentRun?: string
  /** The run's page's tab; Overview when absent. */
  agentRunTab?: Exclude<AdkRunTab, "overview">
}
