import * as React from "react"
import type { StoreApi } from "zustand"

import {
  BuilderProvider as KitProvider,
  createBuilderStore,
  graphOf as kitGraphOf,
  useBuilder as useKitBuilder,
  useBuilderApi as useKitBuilderApi,
  type BuilderState,
  type FlowEdge,
  type FlowNode,
} from "@/components/builder/store"
import { WORKFLOW_ADAPTER } from "@/lib/workflows/adapter"
import { toDocument, type WorkflowDocument, type WorkflowGraph } from "@/lib/workflows/document"
import type { StepData } from "@/lib/workflows/model"
import type { Scope } from "@/lib/workflows/scope"
import type { ValidationContext } from "@/lib/workflows/validate"

/*
 * The workflow builder's state: the builder kit's store
 * (components/builder/store) with the workflow adapter, typed for
 * workflow steps. The workflow builder's own components read it through
 * these; the kit's read the same store.
 */

export {
  selectedStep,
  type BuilderView,
  type CanvasFocus,
  type PendingConnection,
} from "@/components/builder/store"

export type StepNode = FlowNode<StepData>
export type StepEdge = FlowEdge

/** Names for the IDs steps hold, to show them by name. */
export type Lookups = {
  workflows: Record<string, string>
  /** The models Agent steps may run on (`provider/model`), and the default as "". */
  models: Record<string, string>
}

export type WorkflowBuilderState = BuilderState<
  StepData,
  WorkflowDocument,
  ValidationContext,
  Scope,
  Lookups
>

type BuilderInit = { doc: WorkflowDocument; context: ValidationContext }

/** The builder's store for a workflow; the Provider makes one per builder. */
export const createWorkflowBuilderStore = (init: BuilderInit) =>
  createBuilderStore({ adapter: WORKFLOW_ADAPTER, ...init }) as unknown as StoreApi<WorkflowBuilderState>

export function BuilderProvider({
  initial,
  children,
}: {
  initial: BuilderInit
  children?: React.ReactNode
}) {
  return React.createElement(KitProvider, { initial: { adapter: WORKFLOW_ADAPTER, ...initial } }, children)
}

/** Subscribes to the workflow builder's store. */
export function useBuilder<T>(selector: (state: WorkflowBuilderState) => T): T {
  return useKitBuilder(selector as unknown as (state: BuilderState) => T)
}

/** The workflow builder's store itself, for reading or writing outside render. */
export const useBuilderApi = () => useKitBuilderApi() as unknown as StoreApi<WorkflowBuilderState>

/** The graph the builder holds, in the document's terms. */
export const graphOf = (nodes: StepNode[], edges: StepEdge[]): WorkflowGraph => kitGraphOf(nodes, edges)

/** The document the builder holds, as it would save or export it. */
export function documentOf(state: Pick<WorkflowBuilderState, "meta" | "nodes" | "edges">) {
  return toDocument(state.meta, graphOf(state.nodes, state.edges))
}
