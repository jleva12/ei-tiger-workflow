import {
  kindCatalog,
  outputsOfKind,
  type Catalog,
  type CatalogKind,
} from "@/features/builder/lib/catalog"
import type {
  KindGroup,
  KindInfo,
  StepOutput,
} from "@/features/builder/lib/types"
// Types only: the model reads its kinds from here.
import type { AgentConfigs, AgentKind } from "./model"

/*
 * The kinds of node a workflow can have, as the admin API serves them
 * (`GET /workflow-node-kinds`, the workflows task's node catalog,
 * packages/python/adk-workflows' node_kinds.json): each kind's words, its
 * library group, its ways out and what a new one starts with, in the
 * library's order (builder/lib/catalog). Nothing in the console knows them
 * otherwise: the builder and the run pages read them from here once the
 * catalog is installed (NodeCatalogGate does, as it loads; a test installs
 * it itself).
 */

/**
 * Ways out a node's settings list, one per row, before its fixed ones: a
 * Switch's cases, a Match's rules. Each row's `id` is its way out's; with
 * `ways`, each row has those (a Decision board's choice its Accepted and
 * Rejected ways), `<row id>:<way id>`.
 */
export type CatalogRows = {
  /** The setting holding the rows. */
  setting: string
  /** The row's key its way out is called by. */
  label: string
  /** What it's called when that's blank; `{n}` is the row's number. */
  empty: string
  /** Each row's ways out, when it has more than one. */
  ways?: StepOutput[] | null
}

/** One kind of workflow node, as served. */
export type WorkflowKind = CatalogKind & { rows?: CatalogRows | null }

/** What `GET /workflow-node-kinds` answers. */
export type NodeCatalog = Catalog<WorkflowKind>

/** A kind as the library, the canvas and the settings show it. */
export type AgentKindInfo<K extends AgentKind = AgentKind> = KindInfo & {
  defaults: () => AgentConfigs[K]
}

/**
 * The kinds the console can set up: its settings and checks are written
 * for these. A served kind that isn't one is left out.
 */
const SET_UP: Record<AgentKind, true> = {
  start: true,
  llm: true,
  sequential: true,
  parallel: true,
  loop_agent: true,
  saved: true,
  pi_agent: true,
  approval: true,
  human_input: true,
  decision_board: true,
  http: true,
  bash: true,
  transform: true,
  filter: true,
  delay: true,
  git_clone: true,
  git_branch: true,
  git_push: true,
  github_pull_request: true,
  if: true,
  switch: true,
  match: true,
  loop: true,
  for_each: true,
  merge: true,
  end: true,
}

const catalog = kindCatalog<AgentKind, WorkflowKind>(
  "workflow node catalog",
  SET_UP
)

/** Every kind the console can set up, served or not: the format's. */
export const KNOWN_KINDS = catalog.known

/** Makes `served` the one every kind is read from. */
export const installNodeCatalog = (served: NodeCatalog) =>
  catalog.install(served)

export const nodeCatalogInstalled = catalog.installed

/** Whether a kind is served and the console can set it up. */
export const isServedKind = catalog.isServed

/** A served kind, as the library, the canvas and the settings show it. */
export const kindInfo = <K extends AgentKind>(kind: K) =>
  catalog.info(kind) as unknown as AgentKindInfo<K>

/** Every served kind the console can set up, in the library's order. */
export const servedKinds = catalog.list

/** The same, by kind: what the builder kit reads. */
export const servedKindInfos = catalog.infos

/** The library's groups, in order. */
export const kindGroups = (): KindGroup[] => catalog.groups()

function rowsOf(rows: CatalogRows, config: object): StepOutput[] {
  const found = (config as Record<string, unknown>)[rows.setting]
  if (!Array.isArray(found)) return []
  return found.flatMap((row: unknown, index): StepOutput[] => {
    if (!row || typeof row !== "object") return []
    const { id, [rows.label]: label } = row as Record<string, unknown>
    if (typeof id !== "string" || !id) return []
    const named =
      (typeof label === "string" ? label.trim() : "") ||
      rows.empty.replace("{n}", String(index + 1))
    if (rows.ways?.length)
      return rows.ways.map((way) => ({
        ...way,
        id: `${id}:${way.id}`,
        // The way first, so a long label never hides which it is.
        label: `${way.label} · ${named}`,
      }))
    return [{ id, label: named, branch: true }]
  })
}

/**
 * A node's ways out, in the order the canvas lists them: one per row of
 * its settings for a kind whose rows are ways out (a Switch's cases), then
 * its kind's own.
 */
export function waysOutOf(kind: AgentKind, config: object): StepOutput[] {
  const served = catalog.entry(kind)
  return [
    ...(served.rows ? rowsOf(served.rows, config) : []),
    ...outputsOfKind(served),
  ]
}
