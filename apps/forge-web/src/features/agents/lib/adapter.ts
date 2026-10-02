import type { BuilderAdapter } from "@/features/builder/lib/adapter"
import { meaningsFor, type EdgeTone } from "@/features/builder/lib/connections"
import { tidyGraph } from "@/features/builder/lib/layout"
import type { KindInfo, StepOutput } from "@/features/builder/lib/types"
import {
  toDocument,
  toGraph,
  type ChatAgentDocument,
  type ChatAgentGraph,
} from "./document"
import {
  accepts,
  CHAT_GROUPS,
  CHAT_KIND_LIST,
  CHAT_KINDS,
  copyNode,
  HANDS_OFF,
  isChatKind,
  newNode,
  nextNodeId,
  outputsOf,
  TOOLS,
  type ChatStep,
} from "./model"
import { validateChatAgent, type ChatAgentValidationContext } from "./validate"

// A chat agent has no loops with a body: nothing comes back.
const noLoops = () => new Map<string, Set<string>>()

/** Handing off takes a colour of its own; what an agent calls is graphite. */
const toneOf = (_step: ChatStep, output: StepOutput): EdgeTone =>
  output.id === HANDS_OFF ? "case-2" : "neutral"

/**
 * What the builder kit needs to build a chat agent: its kinds of node, the
 * one chat agent (no way in), which way out takes which kind (only agents
 * are handed off to), its document, its checks and how it's laid out and
 * drawn. It reads no run data, so a node's scope is nothing.
 */
export const CHAT_AGENT_ADAPTER: BuilderAdapter<
  ChatStep,
  ChatAgentDocument,
  ChatAgentValidationContext,
  undefined
> = {
  kinds: CHAT_KINDS as Record<string, KindInfo>,
  kindList: CHAT_KIND_LIST,
  groups: CHAT_GROUPS,
  isKind: isChatKind,
  newStep: (kind, steps) => newNode(kind, steps),
  nextStepId: nextNodeId,
  copyStep: copyNode,
  outputsOf,
  hasInput: (kind) => kind !== "agent",
  accepts,
  unique: (kind) => kind === "agent",
  // Nothing goes in between: what's attached leads nowhere.
  insertable: () => false,
  named: () => true,
  // The chat agent is its agent: one name for both.
  namesDocument: (step) => step.kind === "agent",
  metaOf: (doc) => ({
    id: doc.id,
    // The agent's, where the two came apart (kept before they were one).
    name: doc.nodes.find((n) => n.kind === "agent")?.name ?? doc.name,
    description: doc.description,
    organization_id: doc.organization_id,
    created_at: doc.created_at,
    updated_at: doc.updated_at,
  }),
  toGraph,
  toDocument,
  check: (graph, context) => ({
    scopeOf: () => undefined,
    issues: validateChatAgent(graph, context),
  }),
  tidy: (graph: ChatAgentGraph, measure) =>
    tidyGraph(graph, measure, {
      outputsOf,
      loopBodies: noLoops,
      isRoot: (step) => step.kind === "agent",
    }),
  meaningsOf: (graph, edgeId) => {
    const meanings = meaningsFor(graph, edgeId, {
      outputsOf,
      toneOf,
      loopBodies: noLoops,
    })
    // An agent has many tools: their lines go unlabelled, hand-offs say so.
    for (const c of graph.connections) {
      const meaning = meanings.get(edgeId(c))
      if (meaning && c.output === TOOLS)
        meanings.set(edgeId(c), { ...meaning, label: "" })
    }
    return meanings
  },
  loopBodies: noLoops,
  toneOf,
  lookups: () => ({ agents: {}, workflows: {}, models: {} }),
}
