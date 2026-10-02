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
} from "@/components/builder/store"
import { AGENT_ADAPTER } from "@/lib/agents/adapter"
import {
  toDocument,
  type AgentDocument,
  type AgentGraph,
} from "@/lib/agents/document"
import type { AgentStep } from "@/lib/agents/model"
import type { AgentValidationContext } from "@/lib/agents/validate"
import type { Scope } from "@/lib/steps/scope"

/*
 * The agent builder's state: the builder kit's store
 * (components/builder/store) with the agent adapter, typed for agent
 * nodes. The agent builder's own components read it through these; the
 * kit's read the same store.
 */

export type AgentNodeView = FlowNode<AgentStep>
export type AgentEdgeView = FlowEdge

/** Names for the IDs nodes hold, to show them by name. */
export type AgentLookups = {
  /** The organization's agents, by ID. */
  agents: Record<string, string>
  /** The models LLM agents may run on (`provider/model`), and the default as "". */
  models: Record<string, string>
}

export type AgentBuilderState = BuilderState<
  AgentStep,
  AgentDocument,
  AgentValidationContext,
  Scope,
  AgentLookups
>

type AgentBuilderInit = { doc: AgentDocument; context: AgentValidationContext }

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
