import type { BuilderAdapter } from "@/lib/builder/adapter"
import { meaningsFor, type EdgeTone } from "@/lib/builder/connections"
import { tidyGraph } from "@/lib/builder/layout"
import type { KindInfo, StepOutput } from "@/lib/builder/types"
import {
  loopBodies as stepLoopBodies,
  toneOf as stepToneOf,
} from "@/lib/steps/connections"
import type { StepGraph } from "@/lib/steps/document"
import type { StepData } from "@/lib/steps/model"
import type { Scope } from "@/lib/steps/scope"
import {
  toDocument,
  toGraph,
  type AgentDocument,
  type AgentGraph,
} from "./document"
import {
  AGENT_GROUPS,
  AGENT_KIND_LIST,
  AGENT_KINDS,
  copyNode,
  isAgentKind,
  newNode,
  nextNodeId,
  outputsOf,
  type AgentStep,
} from "./model"
import { agentScopesOf } from "./scope"
import { validateAgent, type AgentValidationContext } from "./validate"

// Forge's steps keep their kind names here, so a loop's body and each
// way's tone are found as in a workflow (a Loop agent runs its
// sub-agents inside itself: no body on the canvas).
const loopBodies = (graph: AgentGraph) =>
  stepLoopBodies(graph as unknown as StepGraph)

const toneOf = (step: AgentStep, output: StepOutput, index: number): EdgeTone =>
  stepToneOf(step as StepData, output, index)

/**
 * What the builder kit needs to build an agent: its kinds of node, the
 * one start (no way in; not in between two others), its document, its
 * checks and how it's laid out and drawn.
 */
export const AGENT_ADAPTER: BuilderAdapter<
  AgentStep,
  AgentDocument,
  AgentValidationContext,
  Scope
> = {
  kinds: AGENT_KINDS as Record<string, KindInfo>,
  kindList: AGENT_KIND_LIST,
  groups: AGENT_GROUPS,
  isKind: isAgentKind,
  newStep: (kind, steps) => newNode(kind, steps),
  nextStepId: nextNodeId,
  copyStep: copyNode,
  outputsOf,
  hasInput: (kind) => kind !== "start",
  unique: (kind) => kind === "start",
  insertable: (kind) => kind !== "start",
  named: (kind) => kind !== "start",
  metaOf: (doc) => ({
    id: doc.id,
    name: doc.name,
    description: doc.description,
    organization_id: doc.organization_id,
    created_at: doc.created_at,
    updated_at: doc.updated_at,
  }),
  toGraph,
  toDocument,
  check: (graph, context) => {
    const scopeOf = agentScopesOf(graph)
    return { scopeOf, issues: validateAgent(graph, context, scopeOf) }
  },
  tidy: (graph: AgentGraph, measure) =>
    tidyGraph(graph, measure, {
      outputsOf,
      loopBodies,
      isRoot: (step) => step.kind === "start",
    }),
  meaningsOf: (graph, edgeId) =>
    meaningsFor(graph, edgeId, { outputsOf, toneOf, loopBodies }),
  loopBodies,
  toneOf,
  lookups: () => ({ agents: {}, models: {} }),
}
