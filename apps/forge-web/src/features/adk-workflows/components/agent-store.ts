import type { VersionChoice } from "@/features/builder/components/version-field"
import type {
  KnowledgeBaseLookup,
  McpServerLookup,
} from "@/features/agents/components/chat-agent-store"
import type { ChatAgentLookup } from "@/features/adk-workflows/lib/validate"
import * as React from "react"
import type { StoreApi } from "zustand"

import {
  BuilderProvider as KitProvider,
  graphOf as kitGraphOf,
  useBuilder as useKitBuilder,
  useBuilderApi as useKitBuilderApi,
  type BuilderState,
  type FlowEdge,
  type FlowNode,
} from "@/features/builder/components/store"
import { AGENT_ADAPTER } from "@/features/adk-workflows/lib/adapter"
import {
  toDocument,
  type AgentDocument,
  type AgentGraph,
} from "@/features/adk-workflows/lib/document"
import type { AgentStep } from "@/features/adk-workflows/lib/model"
import type { AgentValidationContext } from "@/features/adk-workflows/lib/validate"
import type { Scope } from "@/features/steps/lib/scope"

/*
 * The agent builder's state: the builder kit's store
 * (builder/components/store) with the agent adapter, typed for agent
 * nodes. The agent builder's own components read it through these; the
 * kit's read the same store.
 */

export type AgentNodeView = FlowNode<AgentStep>
export type AgentEdgeView = FlowEdge

/** Names for the IDs nodes hold, to show them by name. */
export type AgentLookups = {
  /** The organization's agents (its workflows), by ID. */
  agents: Record<string, string>
  /** Where each workflow is between draft and published, for its version picker. */
  workflowVersions: Record<string, VersionChoice>
  /** The models LLM agents may run on (`provider/model`), and the default as "". */
  models: Record<string, string>
  /** The organization's agents from the Agents page, by ID: what an LLM node may be or call. */
  chatAgents: Record<string, ChatAgentChoice>
  /** The organization's MCP servers and knowledge bases, for LLM agents' tools. */
  mcpServers: Record<string, McpServerLookup>
  knowledgeBases: Record<string, KnowledgeBaseLookup>
}

/** An agent from the Agents page, as an LLM node picks it. */
export type ChatAgentChoice = ChatAgentLookup & {
  /** The version its draft will be published as; null without a draft. */
  draftVersion: number | null
}

export type AgentBuilderState = BuilderState<
  AgentStep,
  AgentDocument,
  AgentValidationContext,
  Scope,
  AgentLookups
>

type AgentBuilderInit = {
  doc: AgentDocument
  context: AgentValidationContext
  /** A published version (or a workflow the person can't change): shown, never changed. */
  readOnly?: boolean
}

export function AgentBuilderProvider({
  initial,
  children,
}: {
  initial: AgentBuilderInit
  children?: React.ReactNode
}) {
  return React.createElement(
    KitProvider,
    { initial: { adapter: AGENT_ADAPTER, ...initial } },
    children
  )
}

/** Subscribes to the agent builder's store. */
export function useAgentBuilder<T>(
  selector: (state: AgentBuilderState) => T
): T {
  return useKitBuilder(selector as unknown as (state: BuilderState) => T)
}

/** The agent builder's store itself, for reading or writing outside render. */
export const useAgentBuilderApi = () =>
  useKitBuilderApi() as unknown as StoreApi<AgentBuilderState>

/** The graph the builder holds, in the document's terms. */
export const agentGraphOf = (
  nodes: AgentNodeView[],
  edges: AgentEdgeView[]
): AgentGraph => kitGraphOf(nodes, edges)

/** The document the builder holds, as it would save or export it. */
export function agentDocumentOf(
  state: Pick<AgentBuilderState, "meta" | "nodes" | "edges">
) {
  return toDocument(state.meta, agentGraphOf(state.nodes, state.edges))
}
