import {
  applyEdgeChanges,
  applyNodeChanges,
  type Edge,
  type EdgeChange,
  type Node,
  type NodeChange,
} from "@xyflow/react"
import { createStore, type StoreApi } from "zustand"

import { createContextStore } from "@/lib/context-store"
import type { BaseMeta, BuilderAdapter, Lookups } from "@/features/builder/lib/adapter"
import type { Measure } from "@/features/builder/lib/layout"
import {
  edgeId,
  INPUT,
  type BaseStep,
  type BuilderGraph,
  type BuilderIssue,
  type Point,
} from "@/features/builder/lib/types"

/*
 * A builder's state: what's being built (a workflow, an agent) as React
 * Flow nodes and edges, what's selected, the step whose settings are open,
 * the Canvas / JSON view, undo and redo, and the drag in progress. The
 * library (in the sidebar), the canvas, the step's settings, the details
 * and the top bar all read and change it. What it builds comes from its
 * adapter (builder/lib/adapter): the same store serves every builder.
 */

export type FlowNode<S extends BaseStep = BaseStep> = Node<S, "step">
export type FlowEdge = Edge<{ label?: string; branch?: boolean }, "step">

export type BuilderView = "canvas" | "json"

type Snapshot<S extends BaseStep> = {
  meta: BaseMeta
  nodes: FlowNode<S>[]
  edges: FlowEdge[]
}

/** A branch dragged out onto empty canvas: the step picker opens there. */
export type PendingConnection = {
  source: string
  output: string
  /** Where on the canvas the new step goes. */
  at: Point
  /** Where on screen the picker opens, relative to the canvas. */
  screen: Point
}

/**
 * What the pointer is over on the canvas, a step or a connection: its
 * connections stand out and the rest fade, to follow them.
 */
export type CanvasFocus = { step: string } | { edge: string }

export type BuilderInit<
  S extends BaseStep = BaseStep,
  Doc = unknown,
  Context = unknown,
  Scope = unknown,
> = {
  adapter: BuilderAdapter<S, Doc, Context, Scope>
  doc: Doc
  context: Context
  /** Shown, never changed: a published version, or for someone who can't edit. */
  readOnly?: boolean
}

export type BuilderState<
  S extends BaseStep = BaseStep,
  Doc = unknown,
  Context = unknown,
  Scope = unknown,
  L extends Lookups = Lookups,
> = {
  adapter: BuilderAdapter<S, Doc, Context, Scope>
  meta: BaseMeta
  nodes: FlowNode<S>[]
  edges: FlowEdge[]
  issues: BuilderIssue[]
  /** What a step can read when it runs, and the types of it. */
  scopeOf: (id: string) => Scope
  context: Context
  lookups: L
  view: BuilderView
  past: Snapshot<S>[]
  future: Snapshot<S>[]
  /** The last change's coalescing key: typing in a field is one undo. */
  lastKey: string | null
  lastAt: number
  /** The connection a dragged library step would be dropped into. */
  dropEdge: string | null
  focus: CanvasFocus | null
  pending: PendingConnection | null
  /** Asks the canvas to fit the view (a counter it watches). */
  fitRequest: number
  /** Asks the canvas to bring steps into view: a step just added, and the one it follows. */
  reveal: { ids: string[] } | null
  /** The step whose settings are open, over the canvas. */
  editing: string | null
  /**
   * The setting its settings bring into view as they open (an issue's,
   * clicked): its key, as `BuilderIssue.field`. Cleared once shown.
   */
  focusField: string | null
  /** Whether the details panel shows, beside the canvas (over it in narrow builders). Tucked away at first. */
  details: boolean
  /**
   * Shown, never changed: every change to the document (adding, connecting,
   * moving, editing, undo) does nothing. Selecting and opening steps still work.
   */
  readOnly: boolean

  setView: (view: BuilderView) => void
  /** Changes some of what checking depends on; the rest stays. */
  setContext: (patch: Partial<Context>) => void
  setLookups: (lookups: Partial<L>) => void
  onNodesChange: (changes: NodeChange<FlowNode<S>>[]) => void
  onEdgesChange: (changes: EdgeChange<FlowEdge>[]) => void
  /** Remember the graph before a change the user can undo. */
  checkpoint: (key?: string) => void
  select: (id: string | null) => void
  /** Open a step's settings (selecting it), at one of them if named, or close them. */
  edit: (id: string | null, field?: string) => void
  setDetails: (open: boolean) => void
  addStep: (
    kind: S["kind"],
    at: Point,
    link?: { source: string; output: string } | { edge: string }
  ) => string
  connect: (source: string, output: string, target: string) => void
  removeEdge: (id: string) => void
  removeSteps: (ids: string[]) => void
  duplicateStep: (id: string) => string | undefined
  updateStep: (id: string, change: (data: S) => S, key?: string) => void
  updateMeta: (
    patch: Partial<Pick<BaseMeta, "name" | "description">>,
    key?: string
  ) => void
  replaceDocument: (doc: Doc) => void
  arrange: (measure: Measure) => void
  setDropEdge: (id: string | null) => void
  setFocus: (focus: CanvasFocus | null) => void
  setPending: (pending: PendingConnection | null) => void
  requestFit: () => void
  undo: () => void
  redo: () => void
}

const HISTORY = 100
// The room a new step keeps from the step it follows.
const STEP_GAP = 64
const widthOf = (node: FlowNode) => node.measured?.width ?? 240

/** Every step reachable from `from` (itself included), by connections. */
function downstream(from: string, edges: FlowEdge[]) {
  const seen = new Set<string>()
  const queue = [from]
  while (queue.length) {
    const id = queue.shift()!
    if (seen.has(id)) continue
    seen.add(id)
    for (const e of edges) if (e.source === id) queue.push(e.target)
  }
  return seen
}
// Edits of one field within this long are one step of undo.
const COALESCE_MS = 1200

/** The graph the builder holds, in the document's terms. */
export function graphOf<S extends BaseStep>(
  nodes: FlowNode<S>[],
  edges: FlowEdge[]
): BuilderGraph<S> {
  return {
    steps: nodes.map((n) => ({ id: n.id, data: n.data, position: n.position })),
    connections: edges.map((e) => ({
      source: e.source,
      output: e.sourceHandle ?? "next",
      target: e.target,
    })),
  }
}

/** The document the builder holds, as it would save or export it. */
export function documentOf<S extends BaseStep, Doc>(
  state: Pick<BuilderState<S, Doc>, "adapter" | "meta" | "nodes" | "edges">
): Doc {
  return state.adapter.toDocument(state.meta, graphOf(state.nodes, state.edges))
}

/** The one selected step, if one is. */
export const selectedStep = <S extends BaseStep>(state: {
  nodes: FlowNode<S>[]
}) => {
  const picked = state.nodes.filter((n) => n.selected)
  return picked.length === 1 ? picked[0] : undefined
}

/** The builder's store for a document; the Provider makes one per builder. */
export function createBuilderStore<S extends BaseStep, Doc, Context, Scope>({
  adapter,
  doc,
  context,
  readOnly = false,
}: BuilderInit<S, Doc, Context, Scope>): StoreApi<
  BuilderState<S, Doc, Context, Scope>
> {
  type State = BuilderState<S, Doc, Context, Scope>
  type StepNode = FlowNode<S>

  const toNodes = (graph: BuilderGraph<S>): StepNode[] =>
    graph.steps.map((s) => ({
      id: s.id,
      type: "step",
      position: s.position,
      data: s.data,
    }))

  const makeEdge = (
    source: string,
    output: string,
    target: string,
    nodes: StepNode[]
  ): FlowEdge => {
    const from = nodes.find((n) => n.id === source)
    const info = from
      ? adapter.outputsOf(from.data).find((o) => o.id === output)
      : undefined
    return {
      id: edgeId(source, output, target),
      type: "step",
      source,
      sourceHandle: output,
      target,
      targetHandle: INPUT,
      data: { label: info?.label, branch: info?.branch },
    }
  }

  const toEdges = (graph: BuilderGraph<S>, nodes: StepNode[]): FlowEdge[] =>
    graph.connections.map((c) => makeEdge(c.source, c.output, c.target, nodes))

  /**
   * Edges whose source output no longer exists (a case removed) are dropped,
   * and branch labels follow renamed cases.
   */
  const reconcileEdges = (nodes: StepNode[], edges: FlowEdge[]) => {
    const byId = new Map(nodes.map((n) => [n.id, n]))
    return edges.flatMap((edge) => {
      const from = byId.get(edge.source)
      if (!from || !byId.has(edge.target)) return []
      const info = adapter
        .outputsOf(from.data)
        .find((o) => o.id === edge.sourceHandle)
      if (!info) return []
      if (edge.data?.label === info.label && edge.data?.branch === info.branch)
        return [edge]
      return [{ ...edge, data: { label: info.label, branch: info.branch } }]
    })
  }

  return createStore<State>()((set, get) => {
    const graph = adapter.toGraph(doc)
    const nodes = toNodes(graph)
    const edges = toEdges(graph, nodes)

    const snapshot = (): Snapshot<S> => {
      const { meta, nodes, edges } = get()
      return { meta, nodes, edges }
    }
    /**
     * Commit a change to the graph: recheck it. When the step whose
     * settings are open is gone (removed, undone), they close.
     */
    const commit = (patch: Partial<Snapshot<S>>) => {
      const next = { ...snapshot(), ...patch }
      const { editing } = get()
      set({
        ...patch,
        ...adapter.check(graphOf(next.nodes, next.edges), get().context),
        ...(editing && !next.nodes.some((n) => n.id === editing)
          ? { editing: null }
          : {}),
      })
    }

    const state: State = {
      adapter,
      meta: adapter.metaOf(doc),
      nodes,
      edges,
      ...adapter.check(graphOf(nodes, edges), context),
      context,
      lookups: adapter.lookups(),
      view: "canvas",
      past: [],
      future: [],
      lastKey: null,
      lastAt: 0,
      dropEdge: null,
      focus: null,
      pending: null,
      fitRequest: 0,
      reveal: null,
      editing: null,
      focusField: null,
      details: false,
      readOnly,

      setView: (view) => set({ view }),
      setContext: (patch) => {
        set({ context: { ...get().context, ...patch } })
        commit({})
      },
      setLookups: (lookups) =>
        set({ lookups: { ...get().lookups, ...lookups } as Lookups }),

      onNodesChange: (allChanges) => {
        // Read-only: a step can be selected, and measured, but not moved or removed.
        const changes = get().readOnly
          ? allChanges.filter((c) => c.type === "select" || c.type === "dimensions")
          : allChanges
        const structural = changes.some(
          (c) => c.type === "remove" || c.type === "add"
        )
        if (structural) get().checkpoint()
        const nextNodes = applyNodeChanges(changes, get().nodes)
        if (structural) {
          commit({
            nodes: nextNodes,
            edges: reconcileEdges(nextNodes, get().edges),
          })
        } else {
          set({ nodes: nextNodes })
        }
      },
      onEdgesChange: (allChanges) => {
        const changes = get().readOnly
          ? allChanges.filter((c) => c.type === "select")
          : allChanges
        const structural = changes.some(
          (c) => c.type === "remove" || c.type === "add"
        )
        if (structural) {
          get().checkpoint()
          commit({ edges: applyEdgeChanges(changes, get().edges) })
        } else {
          set({ edges: applyEdgeChanges(changes, get().edges) })
        }
      },

      checkpoint: (key) => {
        const now = Date.now()
        const { lastKey, lastAt, past } = get()
        if (key && key === lastKey && now - lastAt < COALESCE_MS) {
          set({ lastAt: now })
          return
        }
        set({
          past: [...past, snapshot()].slice(-HISTORY),
          future: [],
          lastKey: key ?? null,
          lastAt: now,
        })
      },

      select: (id) =>
        set({
          nodes: get().nodes.map((n) =>
            (n.id === id) === Boolean(n.selected)
              ? n
              : { ...n, selected: n.id === id }
          ),
          edges: get().edges.map((e) =>
            e.selected ? { ...e, selected: false } : e
          ),
        }),
      edit: (id, field) => {
        if (id && !get().nodes.some((n) => n.id === id)) return
        if (id) get().select(id)
        set({ editing: id, focusField: (id && field) || null })
      },
      setDetails: (details) => set({ details }),

      addStep: (kind, at, link) => {
        get().checkpoint()
        const state = get()
        const id = adapter.nextStepId(
          kind,
          state.nodes.map((n) => n.id)
        )
        // It never lands behind the step it follows.
        const sourceId =
          link && "edge" in link
            ? state.edges.find((e) => e.id === link.edge)?.source
            : link?.source
        const source = state.nodes.find((n) => n.id === sourceId)
        const position = source
          ? {
              x: Math.max(at.x, source.position.x + widthOf(source) + STEP_GAP),
              y: at.y,
            }
          : at
        const node: StepNode = {
          id,
          type: "step",
          position,
          data: adapter.newStep(
            kind,
            state.nodes.map((n) => n.data)
          ),
          selected: true,
        }
        let nodes = [
          ...state.nodes.map((n) =>
            n.selected ? { ...n, selected: false } : n
          ),
          node,
        ]
        let edges = state.edges.map((e) =>
          e.selected ? { ...e, selected: false } : e
        )
        if (link && "edge" in link) {
          // Dropped on a connection: the step goes in between.
          const split = edges.find((e) => e.id === link.edge)
          const [first] = adapter.outputsOf(node.data)
          if (split) {
            // Push what comes after right, to make room for it.
            const target = nodes.find((n) => n.id === split.target)
            const shift = target
              ? position.x + widthOf(node) + STEP_GAP - target.position.x
              : 0
            if (shift > 0) {
              const moving = downstream(split.target, edges)
              moving.delete(split.source)
              nodes = nodes.map((n) =>
                moving.has(n.id) && n.id !== id
                  ? {
                      ...n,
                      position: { x: n.position.x + shift, y: n.position.y },
                    }
                  : n
              )
            }
            edges = edges.filter((e) => e.id !== split.id)
            edges.push(
              makeEdge(split.source, split.sourceHandle ?? "next", id, nodes)
            )
            if (first) edges.push(makeEdge(id, first.id, split.target, nodes))
          }
        } else if (link) {
          edges.push(makeEdge(link.source, link.output, id, nodes))
        }
        commit({ nodes, edges })
        set({ reveal: { ids: sourceId ? [sourceId, id] : [id] } })
        return id
      },

      connect: (source, output, target) => {
        if (source === target) return
        const { nodes, adapter } = get()
        const from = nodes.find((n) => n.id === source)
        const to = nodes.find((n) => n.id === target)
        if (from && to && adapter.accepts && !adapter.accepts(from.data, output, to.data.kind)) return
        const id = edgeId(source, output, target)
        if (get().edges.some((e) => e.id === id)) return
        get().checkpoint()
        commit({
          edges: [
            ...get().edges,
            makeEdge(source, output, target, get().nodes),
          ],
        })
      },

      removeEdge: (id) => {
        get().checkpoint()
        commit({ edges: get().edges.filter((e) => e.id !== id) })
      },

      removeSteps: (ids) => {
        if (!ids.length) return
        get().checkpoint()
        const gone = new Set(ids)
        commit({
          nodes: get().nodes.filter((n) => !gone.has(n.id)),
          edges: get().edges.filter(
            (e) => !gone.has(e.source) && !gone.has(e.target)
          ),
        })
      },

      duplicateStep: (id) => {
        const original = get().nodes.find((n) => n.id === id)
        if (!original) return
        get().checkpoint()
        const copyId = adapter.nextStepId(
          original.data.kind,
          get().nodes.map((n) => n.id)
        )
        const copy: StepNode = {
          id: copyId,
          type: "step",
          position: {
            x: original.position.x + 32,
            y: original.position.y + 32,
          },
          data: adapter.copyStep(
            original.data,
            get().nodes.map((n) => n.data)
          ),
          selected: true,
        }
        commit({
          nodes: [
            ...get().nodes.map((n) =>
              n.selected ? { ...n, selected: false } : n
            ),
            copy,
          ],
        })
        return copyId
      },

      updateStep: (id, change, key) => {
        get().checkpoint(key ? `${id}:${key}` : undefined)
        // Renaming the step that is the document renames the document.
        let renamed: string | undefined
        const nodes = get().nodes.map((n) => {
          if (n.id !== id) return n
          const data = change(n.data)
          if (data.name !== n.data.name && adapter.namesDocument?.(data))
            renamed = data.name
          return { ...n, data }
        })
        commit({
          nodes,
          edges: reconcileEdges(nodes, get().edges),
          ...(renamed !== undefined
            ? { meta: { ...get().meta, name: renamed } }
            : {}),
        })
      },

      updateMeta: (patch, key) => {
        get().checkpoint(key ? `meta:${key}` : undefined)
        const meta = { ...get().meta, ...patch }
        // Renaming the document renames the step that is it, in the same step of undo.
        const { name } = patch
        const self =
          name === undefined
            ? undefined
            : get().nodes.find((n) => adapter.namesDocument?.(n.data))
        if (name === undefined || !self || self.data.name === name) {
          set({ meta })
          return
        }
        commit({
          meta,
          nodes: get().nodes.map((n) =>
            n === self ? { ...n, data: { ...n.data, name } } : n
          ),
        })
      },

      replaceDocument: (next) => {
        get().checkpoint()
        const graph = adapter.toGraph(next)
        const nodes = toNodes(graph)
        const { name, description } = adapter.metaOf(next)
        commit({
          meta: { ...get().meta, name, description },
          nodes,
          edges: toEdges(graph, nodes),
        })
      },

      arrange: (measure) => {
        get().checkpoint()
        const positions = adapter.tidy(
          graphOf(get().nodes, get().edges),
          measure
        )
        set({
          nodes: get().nodes.map((n) => {
            const at = positions.get(n.id)
            return at ? { ...n, position: at } : n
          }),
        })
      },

      setDropEdge: (dropEdge) => {
        if (get().dropEdge !== dropEdge) set({ dropEdge })
      },
      setFocus: (focus) => set({ focus }),
      setPending: (pending) => set({ pending }),
      requestFit: () => set({ fitRequest: get().fitRequest + 1 }),

      undo: () => {
        const { past, future } = get()
        const previous = past.at(-1)
        if (!previous) return
        set({
          past: past.slice(0, -1),
          future: [snapshot(), ...future],
          lastKey: null,
        })
        commit(previous)
      },
      redo: () => {
        const { past, future } = get()
        const next = future[0]
        if (!next) return
        set({
          past: [...past, snapshot()],
          future: future.slice(1),
          lastKey: null,
        })
        commit(next)
      },
    }

    // Read-only, every change to the document does nothing.
    const changes = [
      "addStep",
      "connect",
      "removeEdge",
      "removeSteps",
      "duplicateStep",
      "updateStep",
      "updateMeta",
      "replaceDocument",
      "arrange",
      "undo",
      "redo",
    ] as const
    for (const key of changes) {
      const act = state[key] as (...args: unknown[]) => unknown
      ;(state as Record<string, unknown>)[key] = (...args: unknown[]) =>
        get().readOnly ? undefined : act(...args)
    }
    return state
  })
}

/*
 * One Provider for every builder: the kit's components read the store
 * through these, whatever it builds. A builder's own components read it
 * through its typed facade (adk-workflows/components/agent-store).
 */
export const {
  Provider: BuilderProvider,
  useStore: useBuilder,
  useStoreApi: useBuilderApi,
} = createContextStore(
  (init: BuilderInit) =>
    createBuilderStore(init) as unknown as StoreApi<BuilderState>,
  { name: "Builder" }
)

export type { BaseMeta, Lookups }
