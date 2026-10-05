import * as React from "react"
import {
  Background,
  BackgroundVariant,
  MiniMap,
  Panel,
  ReactFlow,
  useNodesInitialized,
  useReactFlow,
  useStore as useFlowStore,
  type Connection,
  type EdgeTypes,
  type FinalConnectionState,
  type NodeTypes,
  type ReactFlowInstance,
} from "@xyflow/react"
import { FitToScreenIcon, MinusSignIcon } from "@hugeicons/core-free-icons"

import { Icon } from "@/components/forge/icon"
import { Button } from "@/components/ui/button"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import type { KindInfo } from "@/features/builder/lib/types"
import { INPUT } from "@/features/builder/lib/types"
import { EdgeLooksContext, useEdgeLooks } from "./edge-looks"
import { StepEdgeView } from "./edge-view"
import { FlowNodeView } from "./node-view"
import { StepPicker } from "./picker"
import {
  useBuilder,
  useBuilderApi,
  type FlowEdge,
  type FlowNode,
} from "./store"
import { capitalized, useBuilderUi } from "./ui"
import {
  kindOfDrag,
  measureFlow,
  reducedMotion,
  revealSteps,
  STEP_MIME,
} from "./utils"
import "./canvas.css"

const GRID = 8
const snap = (value: number) => Math.round(value / GRID) * GRID
// A dropped step lands with its way in under the pointer.
const DROP_OFFSET = { x: 20, y: 28 }

const nodeTypes: NodeTypes = { step: FlowNodeView }
const edgeTypes: EdgeTypes = { step: StepEdgeView }

/**
 * A builder's canvas: its steps and connections on a dot grid. Steps
 * arrive from the library by drag (onto a connection to go in between) or
 * by click; a branch dragged onto empty canvas opens the step picker there,
 * and onto a step, connects to it. Clicking a step (or Enter on it) opens
 * its settings. Connections are routed together and coloured by what they
 * mean (./edge-routes); hovering a step or a connection brings its lines
 * forward.
 */
export function BuilderCanvas({ needsLayout }: { needsLayout: boolean }) {
  const api = useBuilderApi()
  const adapter = useBuilder((s) => s.adapter)
  const readOnly = useBuilder((s) => s.readOnly)
  const ui = useBuilderUi()
  const nodes = useBuilder((s) => s.nodes)
  const edges = useBuilder((s) => s.edges)
  const onNodesChange = useBuilder((s) => s.onNodesChange)
  const onEdgesChange = useBuilder((s) => s.onEdgesChange)
  const fitRequest = useBuilder((s) => s.fitRequest)
  const pending = useBuilder((s) => s.pending)
  const reveal = useBuilder((s) => s.reveal)
  const details = useBuilder((s) => s.details)
  const flow = useReactFlow<FlowNode, FlowEdge>()
  const initialized = useNodesInitialized()
  const frame = React.useRef<HTMLDivElement>(null)
  const [connecting, setConnecting] = React.useState(false)
  const looks = useEdgeLooks()

  // Once the steps are measured: tidy a workflow that has no positions
  // (the example, an import), then frame it.
  const opened = React.useRef(false)
  React.useEffect(() => {
    if (opened.current || !initialized) return
    opened.current = true
    if (needsLayout) {
      api.getState().arrange(measureFlow(flow))
      api.setState({ past: [] })
    }
    requestAnimationFrame(() => openingView(flow, frame.current, adapter.kinds))
  }, [initialized, needsLayout, api, flow, adapter])

  // A step just added comes into view with the step it follows, once the
  // canvas has measured it.
  React.useEffect(() => {
    if (!reveal) return
    const timer = setTimeout(() => revealSteps(flow, frame.current, reveal.ids), 60)
    return () => clearTimeout(timer)
  }, [reveal, flow])

  const firstFit = React.useRef(true)
  React.useEffect(() => {
    if (firstFit.current) {
      firstFit.current = false
      return
    }
    void flow.fitView({
      padding: 0.2,
      maxZoom: 1,
      duration: reducedMotion() ? 0 : 200,
    })
  }, [fitRequest, flow])

  // A way out may lead to a step with a way in, of a kind it takes.
  const fits = React.useCallback(
    (source: string, output: string | null | undefined, target: string) => {
      const nodes = api.getState().nodes
      const from = nodes.find((n) => n.id === source)
      const to = nodes.find((n) => n.id === target)
      if (!from || !to || !output || !adapter.hasInput(to.data.kind)) return false
      return adapter.accepts?.(from.data, output, to.data.kind) ?? true
    },
    [api, adapter]
  )

  const isValidConnection = React.useCallback(
    (c: Connection | FlowEdge) =>
      c.source !== c.target &&
      c.targetHandle === INPUT &&
      fits(c.source, c.sourceHandle, c.target),
    [fits]
  )

  const onConnect = React.useCallback(
    (c: Connection) => {
      if (c.sourceHandle) api.getState().connect(c.source, c.sourceHandle, c.target)
    },
    [api]
  )

  const onConnectEnd = React.useCallback(
    (event: MouseEvent | TouchEvent, state: FinalConnectionState) => {
      setConnecting(false)
      const { fromNode, fromHandle, toNode, isValid } = state
      if (isValid || !fromNode || !fromHandle?.id || fromHandle.type !== "source") return
      const store = api.getState()
      if (toNode) {
        // Dropped on a step rather than its way in: connect to it anyway.
        if (toNode.id !== fromNode.id && fits(fromNode.id, fromHandle.id, toNode.id)) {
          store.connect(fromNode.id, fromHandle.id, toNode.id)
        }
        return
      }
      const point = "changedTouches" in event ? event.changedTouches[0] : event
      const bounds = frame.current?.getBoundingClientRect()
      if (!bounds) return
      const at = flow.screenToFlowPosition({ x: point.clientX, y: point.clientY })
      store.setPending({
        source: fromNode.id,
        output: fromHandle.id,
        at: { x: snap(at.x + 8), y: snap(at.y - DROP_OFFSET.y) },
        screen: { x: point.clientX - bounds.left, y: point.clientY - bounds.top },
      })
    },
    [api, flow, fits]
  )

  const onDragOver = (event: React.DragEvent) => {
    const kind = kindOfDrag(event.dataTransfer.types, adapter.isKind)
    if (!kind) return
    event.preventDefault()
    event.dataTransfer.dropEffect = "copy"
    let edge: string | null = null
    if (adapter.insertable(kind)) {
      for (const element of document.elementsFromPoint(event.clientX, event.clientY)) {
        const found = element.closest(".react-flow__edge")
        if (found) {
          edge = found.getAttribute("data-id")
          break
        }
      }
    }
    api.getState().setDropEdge(edge)
  }

  const onDrop = (event: React.DragEvent) => {
    const kind = event.dataTransfer.getData(STEP_MIME)
    const store = api.getState()
    const edge = store.dropEdge
    store.setDropEdge(null)
    if (!adapter.isKind(kind)) return
    event.preventDefault()
    const at = flow.screenToFlowPosition({ x: event.clientX, y: event.clientY })
    store.addStep(
      kind,
      { x: snap(at.x - DROP_OFFSET.x), y: snap(at.y - DROP_OFFSET.y) },
      edge ? { edge } : undefined
    )
    frame.current?.querySelector<HTMLElement>(".react-flow__renderer")?.focus()
  }

  const onKeyDown = (event: React.KeyboardEvent) => {
    const target = event.target as HTMLElement
    if (target.closest("input, textarea, [contenteditable=true], [role=dialog]")) return
    const store = api.getState()
    if (event.key === "Delete" || event.key === "Backspace") {
      const steps = store.nodes.filter((n) => n.selected).map((n) => n.id)
      const links = store.edges.filter((e) => e.selected).map((e) => e.id)
      if (!steps.length && !links.length) return
      event.preventDefault()
      if (steps.length) store.removeSteps(steps)
      for (const id of links) store.removeEdge(id)
    } else if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "d") {
      const picked = store.nodes.filter((n) => n.selected)
      if (picked.length !== 1) return
      event.preventDefault()
      store.duplicateStep(picked[0].id)
    } else if (event.key === "Escape" && store.pending) {
      store.setPending(null)
    } else if (event.key === "Enter" && target.classList.contains("react-flow__node")) {
      if (!target.dataset.id) return
      event.preventDefault()
      store.edit(target.dataset.id)
    }
  }

  return (
    <div
      ref={frame}
      className="relative size-full min-h-0"
      onKeyDown={onKeyDown}
      onDragLeave={(event) => {
        if (!frame.current?.contains(event.relatedTarget as Node | null)) {
          api.getState().setDropEdge(null)
        }
      }}
    >
      <EdgeLooksContext.Provider value={looks}>
        <ReactFlow<FlowNode, FlowEdge>
          className={connecting ? "forge-flow connecting" : "forge-flow"}
          nodes={nodes}
          edges={edges}
          nodeTypes={nodeTypes}
          edgeTypes={edgeTypes}
          onNodesChange={onNodesChange}
          onEdgesChange={onEdgesChange}
          onConnect={onConnect}
          onConnectStart={() => {
            setConnecting(true)
            api.getState().setPending(null)
          }}
          onConnectEnd={onConnectEnd}
          isValidConnection={isValidConnection}
          onNodeDragStart={() => {
            api.getState().setFocus(null)
            api.getState().checkpoint()
          }}
          onSelectionDragStart={() => api.getState().checkpoint()}
          onPaneClick={() => api.getState().setPending(null)}
          onNodeMouseEnter={(_, node) => api.getState().setFocus({ step: node.id })}
          onNodeMouseLeave={() => api.getState().setFocus(null)}
          onEdgeMouseEnter={(_, edge) => api.getState().setFocus({ edge: edge.id })}
          onEdgeMouseLeave={() => api.getState().setFocus(null)}
          onNodeClick={(event, node) => {
            // ⌘, Ctrl or ⇧ adds to the selection; a way in or out starts a connection.
            if (event.metaKey || event.ctrlKey || event.shiftKey) return
            if ((event.target as Element).closest(".react-flow__handle")) return
            api.getState().edit(node.id)
          }}
          onDragOver={onDragOver}
          onDrop={onDrop}
          // Read-only: steps are looked at and opened, never moved or joined.
          nodesDraggable={!readOnly}
          nodesConnectable={!readOnly}
          deleteKeyCode={null}
          snapToGrid
          snapGrid={[GRID, GRID]}
          zoomOnScroll
          zoomOnPinch
          panOnDrag
          panOnScroll={false}
          minZoom={0.1}
          maxZoom={1.75}
          connectionRadius={28}
          elevateNodesOnSelect
          attributionPosition="top-right"
          aria-label={`${capitalized(ui.nouns.doc)} canvas`}
        >
          <Background
            variant={BackgroundVariant.Dots}
            gap={16}
            size={1.1}
            color="color-mix(in oklch, var(--subtle) 55%, transparent)"
          />
          <ZoomControls />
          <MiniMap
            position="bottom-right"
            pannable
            zoomable
            ariaLabel={`Map of the ${ui.nouns.doc}`}
            nodeBorderRadius={4}
            // With the details tucked away, the assistant's launcher floats
            // over this corner of the canvas: the map sits above it.
            style={{ width: 168, height: 108, marginBottom: details ? 22 : 72 }}
          />
          {nodes.length === 0 && <EmptyCanvas text={ui.emptyCanvas} />}
        </ReactFlow>
      </EdgeLooksContext.Provider>
      {pending && <StepPicker pending={pending} />}
    </div>
  )
}

// The first view's zoom: below this, a step's smallest text (10px) drops
// under the 9px floor.
const OPENING_ZOOM = { min: 0.9, max: 1 }
const MARGIN = 32

/** Where steps stand side by side: the x-extents that overlap, merged, left to right. */
function columnsOf(nodes: FlowNode[]) {
  const spans = nodes
    .map((n) => ({ start: n.position.x, end: n.position.x + (n.measured?.width ?? 240) }))
    .sort((a, b) => a.start - b.start)
  const merged: { start: number; end: number }[] = []
  for (const span of spans) {
    const last = merged.at(-1)
    if (last && span.start <= last.end) last.end = Math.max(last.end, span.end)
    else merged.push({ ...span })
  }
  return merged
}

/**
 * The first view of a graph, at a zoom its text can be read at: all of
 * it when it fits; otherwise as many whole columns of steps as fit, read
 * from the start, but reaching the first agent (it's about its agents),
 * with the canvas's edges falling in the gaps between columns, never
 * through a step. The minimap shows the rest.
 */
function openingView(
  flow: ReactFlowInstance<FlowNode, FlowEdge>,
  frame: HTMLElement | null,
  kinds: Record<string, KindInfo>
) {
  const nodes = flow.getNodes()
  const box = frame?.getBoundingClientRect()
  if (!nodes.length || !box?.width) return
  const columns = columnsOf(nodes)
  const agent = nodes
    .filter((n) => kinds[n.data.kind]?.orb !== undefined)
    .sort((a, b) => a.position.x - b.position.x)[0]
  const agentColumn = agent
    ? columns.findIndex((c) => agent.position.x >= c.start && agent.position.x < c.end)
    : 0
  const gapBefore = (i: number) => (i > 0 ? columns[i].start - columns[i - 1].end : Infinity)
  const gapAfter = (i: number) =>
    i < columns.length - 1 ? columns[i + 1].start - columns[i].end : Infinity
  let pick: { first: number; last: number; zoom: number; left: number } | undefined
  for (let count = columns.length; count >= 1 && !pick; count -= 1) {
    const first = Math.max(0, Math.min(agentColumn - count + 1, columns.length - count))
    const last = first + count - 1
    const span = columns[last].end - columns[first].start
    const zoom = Math.min(OPENING_ZOOM.max, (box.width - MARGIN * 2) / span)
    if (zoom < OPENING_ZOOM.min) continue
    // Split what's left between the margins, each inside its gap.
    const spare = box.width - span * zoom
    const left = Math.min(spare / 2, gapBefore(first) * zoom - 8)
    const right = spare - left
    if (left < 0 || right > gapAfter(last) * zoom - 8) continue
    pick = { first, last, zoom, left }
  }
  const { first, last, zoom, left } = pick ?? {
    first: agentColumn,
    last: agentColumn,
    zoom: OPENING_ZOOM.max,
    left: MARGIN,
  }
  const shown = nodes.filter(
    (n) => n.position.x >= columns[first].start && n.position.x < columns[last].end
  )
  const top = Math.min(...shown.map((n) => n.position.y))
  const bottom = Math.max(...shown.map((n) => n.position.y + (n.measured?.height ?? 64)))
  // Centre what's shown top to bottom; too tall, start at its top.
  const y =
    (bottom - top) * zoom + MARGIN * 2 <= box.height
      ? (box.height - (bottom - top) * zoom) / 2 - top * zoom
      : MARGIN - top * zoom
  void flow.setViewport({ x: left - columns[first].start * zoom, y, zoom })
}

function ZoomControls() {
  const flow = useReactFlow()
  const zoom = useFlowStore((s) => s.transform[2])
  const requestFit = useBuilder((s) => s.requestFit)
  const { nouns } = useBuilderUi()
  const duration = () => (reducedMotion() ? 0 : 150)
  return (
    <Panel position="bottom-left" className="m-3!">
      <div
        role="group"
        aria-label="Zoom"
        className="flex items-center rounded-(--radius-control) border bg-background p-0.5 shadow-(--shadow-raised)"
      >
        <ZoomButton label="Zoom out" onClick={() => void flow.zoomOut({ duration: duration() })}>
          <Icon icon={MinusSignIcon} size={14} />
        </ZoomButton>
        <span className="w-11 text-center text-2xs text-muted-foreground tabular-nums">
          {Math.round(zoom * 100)}%
        </span>
        <ZoomButton label="Zoom in" onClick={() => void flow.zoomIn({ duration: duration() })}>
          <Icon icon="plus" size={14} />
        </ZoomButton>
        <span aria-hidden="true" className="mx-0.5 h-4 w-px bg-border" />
        <ZoomButton label={`Fit the ${nouns.doc}`} onClick={requestFit}>
          <Icon icon={FitToScreenIcon} size={14} />
        </ZoomButton>
      </div>
    </Panel>
  )
}

function ZoomButton({
  label,
  onClick,
  children,
}: {
  label: string
  onClick: () => void
  children: React.ReactNode
}) {
  return (
    <Tooltip>
      <TooltipTrigger
        render={
          <Button
            variant="ghost"
            size="icon-xs"
            aria-label={label}
            onClick={onClick}
            className="text-muted-foreground hover:text-foreground"
          />
        }
      >
        {children}
      </TooltipTrigger>
      <TooltipContent side="top">{label}</TooltipContent>
    </Tooltip>
  )
}

function EmptyCanvas({ text }: { text: string }) {
  return (
    <Panel position="top-center" className="pointer-events-none mt-[22vh]!">
      <div className="flex max-w-80 flex-col items-center gap-2 text-center">
        <span className="grid size-10 place-items-center rounded-(--radius-card) border border-dashed text-subtle">
          <Icon icon="plus" size={18} />
        </span>
        <p className="text-sm font-medium text-foreground">Nothing on the canvas</p>
        <p className="text-xs/[1.6] text-muted-foreground">{text}</p>
      </div>
    </Panel>
  )
}

