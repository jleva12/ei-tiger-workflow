import {
  AiNetworkIcon,
  ChatBotIcon,
  WebhookIcon,
  WorkflowSquare03Icon,
} from "@hugeicons/core-free-icons"

import type { IconProp } from "@/components/forge/icons"
import type { AdkRunTab } from "@/lib/agents/runs"
import type { BackgroundTaskTab } from "@/lib/background-tasks"
import type { EventsTab, EventTypeTab } from "@/lib/events"

/**
 * The pages of an organization's workspace
 * (`/organizations/$organizationId?view=…`), as its sub nav lists them:
 * Workflows (its workflows, each opening its builder at
 * /organizations/$organizationId/workflows/$workflowId, and their runs),
 * ADK workflows (its Google ADK graph workflows, each opening its builder
 * at /organizations/$organizationId/agents/$agentId: `agents` in the URL
 * and the API, from before they were called that), Agents (its chat
 * agents, each opening its builder at
 * /organizations/$organizationId/chat-agents/$chatAgentId), Events (what
 * other systems send the organization) and Members (who holds which role
 * in it; its admins change them). Workflows is the default, so it has no
 * `view` in the URL.
 */
export const WORKSPACE_VIEWS = {
  workflows: { label: "Workflows", icon: WorkflowSquare03Icon },
  agents: { label: "ADK workflows", icon: AiNetworkIcon },
  // The sub nav draws the assistant's mark for it, as its launcher wears.
  "chat-agents": { label: "Agents", icon: ChatBotIcon, mark: "assistant" },
  events: { label: "Events", icon: WebhookIcon },
  config: { label: "Members", icon: "team" },
} as const satisfies Record<
  string,
  { label: string; icon: IconProp; mark?: "assistant" }
>

export type WorkspaceView = keyof typeof WORKSPACE_VIEWS

export const isWorkspaceView = (value: unknown): value is WorkspaceView =>
  typeof value === "string" && Object.hasOwn(WORKSPACE_VIEWS, value)

/**
 * What the workspace's URL says beyond the organization: the page (`view`)
 * and what's open on it. The route reads it (`validateSearch`), and links
 * into the workspace are made of it.
 */
export type WorkspaceSearch = {
  /** The page; Workflows when absent. */
  view?: WorkspaceView
  /** Events' tab; Event types when absent. */
  eventsTab?: Exclude<EventsTab, "types">
  /** On Events, an event type to open (its ID), or `new` to define one. */
  eventType?: string
  /** The event type page's tab; Definition when absent. */
  eventTab?: Exclude<EventTypeTab, "definition">
  /** Workflows' tab: Workflow tasks; Overview when absent. */
  workflowsTab?: "tasks"
  /** On Workflows, a run to open (its background task's ID). */
  workflowTask?: string
  /** The run's page's tab; Overview when absent. */
  workflowTaskTab?: Exclude<BackgroundTaskTab, "overview">
  /** ADK workflows' tab: Runs; Overview when absent. */
  agentsTab?: "runs"
  /** On ADK workflows, a run to open (its background task's ID). */
  agentRun?: string
  /** The run's page's tab; Overview when absent. */
  agentRunTab?: Exclude<AdkRunTab, "overview">
}
