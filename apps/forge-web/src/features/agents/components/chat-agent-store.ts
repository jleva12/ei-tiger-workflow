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
import { CHAT_AGENT_ADAPTER } from "@/features/agents/lib/adapter"
import {
  toDocument,
  type ChatAgentDocument,
  type ChatAgentGraph,
} from "@/features/agents/lib/document"
import type { ChatStep } from "@/features/agents/lib/model"
import type { ChatAgentValidationContext } from "@/features/agents/lib/validate"

/*
 * The chat agent builder's state: the builder kit's store
 * (builder/components/store) with the chat agent adapter, typed for its
 * nodes. The Agents page's components read it through these; the kit's
 * read the same store.
 */

export type ChatNodeView = FlowNode<ChatStep>
export type ChatEdgeView = FlowEdge

/** Names for the IDs nodes hold, to show them by name. */
export type ChatLookups = {
  /** The organization's other agents, by ID. */
  agents: Record<string, string>
  /** The organization's workflows, by ID. */
  workflows: Record<string, string>
  /** The models agents may run on (`provider/model`), and the default as "". */
  models: Record<string, string>
  /** The organization's MCP servers, by ID. */
  mcpServers: Record<string, McpServerLookup>
  /** The organization's knowledge bases, by ID. */
  knowledgeBases: Record<string, KnowledgeBaseLookup>
}

/** A knowledge base, as a knowledge base tool node shows it. */
export type KnowledgeBaseLookup = {
  name: string
  description: string
  /** Its documents, and how many of them are searchable. */
  documents: number
  ready: number
}

/** An MCP server, as an MCP tool node shows it. */
export type McpServerLookup = {
  name: string
  url: string
  /** Its tools' names, when it's been checked. */
  tools: string[] | null
}

export type ChatBuilderState = BuilderState<
  ChatStep,
  ChatAgentDocument,
  ChatAgentValidationContext,
  undefined,
  ChatLookups
>

type ChatBuilderInit = {
  doc: ChatAgentDocument
  context: ChatAgentValidationContext
  /** A published version (or an agent the person can't change): shown, never changed. */
  readOnly?: boolean
}

export function ChatBuilderProvider({
  initial,
  children,
}: {
  initial: ChatBuilderInit
  children?: React.ReactNode
}) {
  return React.createElement(
    KitProvider,
    { initial: { adapter: CHAT_AGENT_ADAPTER, ...initial } },
    children
  )
}

/** Subscribes to the chat agent builder's store. */
export function useChatBuilder<T>(selector: (state: ChatBuilderState) => T): T {
  return useKitBuilder(selector as unknown as (state: BuilderState) => T)
}

/** The chat agent builder's store itself, for reading or writing outside render. */
export const useChatBuilderApi = () =>
  useKitBuilderApi() as unknown as StoreApi<ChatBuilderState>

/** The graph the builder holds, in the document's terms. */
export const chatGraphOf = (
  nodes: ChatNodeView[],
  edges: ChatEdgeView[]
): ChatAgentGraph => kitGraphOf(nodes, edges)

/** The document the builder holds, as it would save or export it. */
export function chatDocumentOf(
  state: Pick<ChatBuilderState, "meta" | "nodes" | "edges">
) {
  return toDocument(state.meta, chatGraphOf(state.nodes, state.edges))
}
