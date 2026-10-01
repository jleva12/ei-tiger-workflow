import type { BuilderAdapter } from "@/lib/builder/adapter"
import type { KindInfo } from "@/lib/builder/types"
import { loopBodies, meaningsOf, toneOf } from "./connections"
import { toDocument, toGraph, type WorkflowDocument } from "./document"
import { tidy } from "./layout"
import {
  isStepKind,
  newStep,
  nextStepId,
  outputsOf,
  STEP_GROUPS,
  STEP_KIND_LIST,
  STEP_KINDS,
  type StepData,
} from "./model"
import { scopesOf, type Scope } from "./scope"
import { validateWorkflow, type ValidationContext } from "./validate"

/**
 * What the builder kit needs to build a workflow: its kinds of step, the
 * one start step (no way in; not in between two others, nor an End), its
 * document, its checks and how it's laid out and drawn.
 */
export const WORKFLOW_ADAPTER: BuilderAdapter<StepData, WorkflowDocument, ValidationContext, Scope> = {
  kinds: STEP_KINDS as Record<string, KindInfo>,
  kindList: STEP_KIND_LIST,
  groups: STEP_GROUPS,
  isKind: isStepKind,
  newStep: (kind) => newStep(kind),
  nextStepId,
  copyStep: (step) => structuredClone({ ...step, name: `${step.name} (copy)` }),
  outputsOf,
  hasInput: (kind) => kind !== "entry",
  unique: (kind) => kind === "entry",
  insertable: (kind) => kind !== "entry" && kind !== "end",
  named: (kind) => kind !== "entry",
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
    const scopeOf = scopesOf(graph)
    return { scopeOf, issues: validateWorkflow(graph, context, scopeOf) }
  },
  tidy,
  meaningsOf,
  loopBodies,
  toneOf,
  lookups: () => ({ workflows: {}, models: {} }),
}
