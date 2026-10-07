import * as React from "react"
import { useQuery } from "@tanstack/react-query"

import { ViewToolbar } from "@/components/forge/app-shell"
import { PageEmpty, PanelEmpty } from "@/components/forge/empty-state"
import { ErrorCallout } from "@/components/forge/feedback"
import { Icon } from "@/components/forge/icon"
import { ShellToolbar } from "@/components/forge/shell"
import { ToolbarButton } from "@/components/forge/toolbar"
import { Button } from "@/components/ui/button"
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible"
import { Input } from "@/components/ui/input"
import {
  InputGroup,
  InputGroupAddon,
  InputGroupButton,
  InputGroupInput,
} from "@/components/ui/input-group"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Skeleton } from "@/components/ui/skeleton"
import { Spinner } from "@/components/ui/spinner"
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { Toggle } from "@/components/ui/toggle"
import {
  isActive,
  type CodeIngestion,
  type CodeRepository,
} from "@/features/code-repositories/lib/api"
import { fullName, shortSha } from "@/features/code-repositories/lib/display"
import {
  codeGraphKeys,
  graphErrorMessage,
  type CodeGraphReads,
} from "../lib/api"
import {
  FAMILY_LABELS,
  lookOf,
  MAX_EDGES,
  MAX_NODES,
  nodeLabel,
  propertyValue,
  START_KINDS,
  typeCounts,
  visibleGraph,
  type GraphData,
  type NodeVersion,
  type Selection,
} from "../lib/graph"
import { FAMILIES } from "./node-look"
import { KindBadge, NodeMark } from "./node-mark"
import { useCodeGraph } from "./use-code-graph"

// Cytoscape is large; load it with the explorer, not with the app.
const GraphCanvas = React.lazy(() =>
  import("./graph-canvas").then((module) => ({ default: module.GraphCanvas }))
)

// A kind's toggle; switched off, its kind is hidden, so it reads as off.
const KIND_TOGGLE =
  "h-6 gap-1.5 px-2 text-2xs aria-[pressed=false]:text-subtle aria-[pressed=false]:[&_[aria-hidden]]:opacity-35"

const KIND_ITEMS = [
  { value: "", label: "Any kind" },
  ...START_KINDS.map((kind) => ({ value: kind, label: kind })),
]

/**
 * An organization's code repository as a code graph, to explore: a sample
 * of its published nodes to start from (of one kind, or any), grown by
 * expanding nodes, searching for symbols and loading more. Everything comes
 * from one published generation, which the toolbar names with its commit.
 * On a wide shell it fills the page: finder, canvas and a resizable
 * inspector side by side; narrower, the canvas goes on top and the page
 * scrolls.
 */
export function CodeGraphExplorer({
  organizationId,
  repository,
  openAt,
  tabs,
  active = true,
}: {
  organizationId: string
  repository: CodeRepository
  /** A node to open at, selected, e.g. one followed from a link. */
  openAt?: string
  /** The page's view tabs, first in the toolbar. */
  tabs?: React.ReactNode
  /** False while another view has the page: the explorer keeps its state but not the toolbar. */
  active?: boolean
}) {
  const [kind, setKind] = React.useState("")
  // Reloading starts a new session, from the live generation.
  const [revision, setRevision] = React.useState(0)
  return (
    <GraphSession
      key={`${kind}:${revision}:${openAt ?? ""}`}
      organizationId={organizationId}
      repository={repository}
      openAt={openAt}
      kind={kind}
      onKindChange={setKind}
      onReload={() => setRevision((v) => v + 1)}
      tabs={tabs}
      active={active}
    />
  )
}

// The finder, canvas and inspector. Wide: three columns filling the page's
// height, the inspector as wide as it's been dragged. Narrower: the canvas
// on top with the finder and inspector under it; on phones, one column.
const WORKBENCH =
  "grid min-h-[30rem] min-w-0 flex-1 gap-3 grid-cols-[16rem_minmax(0,1fr)_var(--inspector-width,clamp(20rem,30%,36rem))] grid-rows-[minmax(0,1fr)] [grid-template-areas:'finder_canvas_inspector'] @max-[1270px]/shell:min-h-0 @max-[1270px]/shell:flex-none @max-[1270px]/shell:grid-cols-2 @max-[1270px]/shell:grid-rows-[28rem_auto] @max-[1270px]/shell:[grid-template-areas:'canvas_canvas'_'finder_inspector'] @max-[600px]/shell:grid-cols-1 @max-[600px]/shell:grid-rows-[24rem_auto_auto] @max-[600px]/shell:[grid-template-areas:'canvas'_'inspector'_'finder']"

const PANEL = "min-h-0 min-w-0 rounded-(--radius-card) border bg-card"

function GraphSession({
  organizationId,
  repository,
  openAt,
  kind,
  onKindChange,
  onReload,
  tabs,
  active,
}: {
  organizationId: string
  repository: CodeRepository
  openAt: string | undefined
  kind: string
  onKindChange: (kind: string) => void
  onReload: () => void
  tabs: React.ReactNode
  active: boolean
}) {
  const graph = useCodeGraph(organizationId, repository.id, kind, openAt)
  const [chosen, setSelection] = React.useState<Selection>(
    openAt ? { type: "node", id: openAt } : null
  )
  const [hiddenNodes, setHiddenNodes] = React.useState<Set<string>>(new Set())
  const [hiddenEdges, setHiddenEdges] = React.useState<Set<string>>(new Set())
  const [filter, setFilter] = React.useState("")
  const [list, setList] = React.useState("nodes")
  const data = graph.data
  // What's chosen, when the loaded graph has it: a node that couldn't be
  // opened selects nothing.
  const selection: Selection =
    chosen &&
    (chosen.type === "node"
      ? data?.nodes.some((v) => v.fact.node.id === chosen.id)
      : data?.edges.some((v) => v.fact.edge.id === chosen.id))
      ? chosen
      : null
  const { gridRef, panelRef, current, max, resize, style } = useInspectorWidth(
    !!data && data.generation > 0
  )
  const visible = React.useMemo(
    () =>
      data
        ? visibleGraph(data, hiddenNodes, hiddenEdges, filter)
        : { nodes: [], edges: [] },
    [data, hiddenNodes, hiddenEdges, filter]
  )
  const selectedNode =
    selection?.type === "node"
      ? data?.nodes.find((v) => v.fact.node.id === selection.id)
      : undefined
  // The selected node's next page of relationships: undefined before the
  // first, null once all are loaded.
  const cursor = selectedNode && graph.cursors[selectedNode.fact.node.id]
  const atLimit =
    !!data && (data.nodes.length >= MAX_NODES || data.edges.length >= MAX_EDGES)

  function resetFilters() {
    setHiddenNodes(new Set())
    setHiddenEdges(new Set())
    setFilter("")
  }
  function toggle(type: string, set: typeof setHiddenNodes) {
    set((current) => {
      const next = new Set(current)
      if (next.has(type)) next.delete(type)
      else next.add(type)
      return next
    })
  }
  function selectNode(node: NodeVersion) {
    graph.add(node)
    resetFilters()
    setSelection({ type: "node", id: node.fact.node.id })
  }

  const toolbar = active && (
    <GraphToolbar
      tabs={tabs}
      data={data}
      counts={
        data &&
        `${visible.nodes.length} of ${data.nodes.length} nodes · ${visible.edges.length} of ${data.edges.length} relationships`
      }
      loading={!data && !graph.error}
      kind={kind}
      onKindChange={onKindChange}
      onReload={onReload}
    />
  )
  if (!data) {
    return graph.error ? (
      <>
        {toolbar}
        <ErrorCallout
          title="Couldn't load the graph"
          action={
            <Button variant="outline" size="sm" onClick={onReload}>
              Retry
            </Button>
          }
        >
          {graph.error}
        </ErrorCallout>
      </>
    ) : (
      <div className="flex h-full flex-col @max-[1270px]/shell:h-auto">
        {toolbar}
        <div
          className={WORKBENCH}
          aria-busy="true"
          aria-label="Loading the graph"
        >
          <Skeleton className="rounded-(--radius-card) [grid-area:finder] @max-[1270px]/shell:h-48" />
          <Skeleton className="rounded-(--radius-card) [grid-area:canvas]" />
          <Skeleton className="rounded-(--radius-card) [grid-area:inspector] @max-[1270px]/shell:h-48" />
        </div>
      </div>
    )
  }
  if (data.generation === 0) {
    return (
      <>
        {toolbar}
        <PageEmpty
          illustration="waiting"
          title="Nothing published yet"
          description={`The code graph has no published generation of ${fullName(repository)}. Ingest it again, and it shows here once the run succeeds.`}
        />
      </>
    )
  }

  return (
    <div className="flex h-full min-w-0 flex-col gap-3 @max-[1270px]/shell:h-auto">
      {toolbar}
      {graph.error && (
        <ErrorCallout title="That didn't load">{graph.error}</ErrorCallout>
      )}
      <IngestionNotice
        repository={repository}
        data={data}
        onReload={onReload}
      />

      <div ref={gridRef} className={WORKBENCH} style={style}>
        <aside
          className={`${PANEL} flex flex-col gap-4 overflow-hidden p-3 [grid-area:finder]`}
          aria-label="Find, filter and list"
        >
          <SymbolFinder
            graph={graph.graph}
            organizationId={organizationId}
            repositoryId={repository.id}
            generation={data.generation}
            onSelect={selectNode}
            disabled={!!graph.busy || atLimit}
          />
          <section className="flex flex-col gap-2">
            <div className="flex items-center justify-between">
              <h3 className="text-xs font-medium text-foreground">
                Loaded kinds
              </h3>
              <Button variant="ghost" size="xs" onClick={resetFilters}>
                Show all
              </Button>
            </div>
            <div className="flex flex-wrap gap-1" aria-label="Node kinds">
              {typeCounts(data.nodes.map((v) => v.fact.node.kind)).map(
                ([type, count]) => (
                  <Toggle
                    key={type}
                    size="sm"
                    variant="outline"
                    aria-label={`${type}, ${count} loaded`}
                    pressed={!hiddenNodes.has(type)}
                    onPressedChange={() => toggle(type, setHiddenNodes)}
                    className={KIND_TOGGLE}
                  >
                    <KindBadge kind={type} size={14} />
                    {type}
                    <span className="text-subtle tabular-nums">{count}</span>
                  </Toggle>
                )
              )}
            </div>
            <h4 className="mt-1 text-2xs text-muted-foreground">
              Relationships
            </h4>
            <div
              className="flex flex-wrap gap-1"
              aria-label="Relationship kinds"
            >
              {typeCounts(data.edges.map((v) => v.fact.edge.kind)).map(
                ([type, count]) => (
                  <Toggle
                    key={type}
                    size="sm"
                    variant="outline"
                    aria-label={`${type}, ${count} loaded`}
                    pressed={!hiddenEdges.has(type)}
                    onPressedChange={() => toggle(type, setHiddenEdges)}
                    className={KIND_TOGGLE}
                  >
                    {type}
                    <span className="text-subtle tabular-nums">{count}</span>
                  </Toggle>
                )
              )}
              {!data.edges.length && (
                <p className="text-2xs text-subtle">
                  Expand a node to load its relationships.
                </p>
              )}
            </div>
          </section>
          <section className="flex min-h-0 flex-1 flex-col gap-2">
            <Input
              aria-label="Filter loaded nodes"
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
              placeholder="Filter loaded nodes…"
              className="h-7 text-xs"
            />
            <Tabs value={list} onValueChange={(v) => setList(String(v))}>
              <TabsList variant="line" aria-label="Loaded records">
                <TabsTrigger value="nodes">Nodes</TabsTrigger>
                <TabsTrigger value="edges">Relationships</TabsTrigger>
              </TabsList>
            </Tabs>
            <div
              tabIndex={0}
              className="-mx-1 flex min-h-0 flex-1 flex-col overflow-y-auto @max-[1270px]/shell:max-h-72"
              aria-label={
                list === "nodes" ? "Loaded nodes" : "Loaded relationships"
              }
            >
              {list === "nodes"
                ? visible.nodes.map(({ fact: { node } }) => (
                    <RecordButton
                      key={node.id}
                      active={selection?.id === node.id}
                      onClick={() =>
                        setSelection({ type: "node", id: node.id })
                      }
                      lead={<KindBadge kind={node.kind} size={18} />}
                      title={nodeLabel(node)}
                      detail={node.kind}
                    />
                  ))
                : visible.edges.map(({ fact: { edge } }) => (
                    <RecordButton
                      key={edge.id}
                      active={selection?.id === edge.id}
                      onClick={() =>
                        setSelection({ type: "edge", id: edge.id })
                      }
                      lead={
                        <Icon icon="link" size={13} className="text-subtle" />
                      }
                      title={edge.kind}
                      detail={`${nameFor(data, edge.source_id)} → ${nameFor(data, edge.target_id)}`}
                    />
                  ))}
            </div>
          </section>
        </aside>

        <div className="relative flex min-h-0 min-w-0 flex-col overflow-hidden rounded-(--radius-card) border bg-background [grid-area:canvas]">
          <div className="min-h-0 flex-1">
            <React.Suspense
              fallback={<Skeleton className="size-full rounded-none" />}
            >
              <GraphCanvas
                nodes={data.nodes}
                edges={data.edges}
                visibleNodes={visible.nodes}
                visibleEdges={visible.edges}
                selection={selection}
                onSelect={setSelection}
                onExpand={(id) => {
                  const node = data.nodes.find((v) => v.fact.node.id === id)
                  if (node && !graph.busy && !atLimit) {
                    setSelection({ type: "node", id })
                    void graph.expand(node)
                  }
                }}
              />
            </React.Suspense>
          </div>
          <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5 border-t bg-card py-1.5 pr-1.5 pl-3 text-2xs text-muted-foreground">
            <Legend kinds={data.nodes.map((v) => v.fact.node.kind)} />
            <div className="ml-auto flex items-center gap-3">
              {atLimit && <span>The canvas is full; focus on a node.</span>}
              <Button
                variant="outline"
                size="xs"
                disabled={!data.next_cursor || !!graph.busy || atLimit}
                onClick={() => void graph.more()}
              >
                <Icon icon="plus" data-icon="inline-start" />
                {data.next_cursor ? "Load more nodes" : "All nodes loaded"}
              </Button>
            </div>
          </div>
          {!visible.nodes.length && (
            <div className="absolute inset-0 flex items-center justify-center">
              <PanelEmpty icon="branch">
                {data.nodes.length
                  ? "No loaded node matches these filters. Show all kinds or clear the filter."
                  : "No nodes of this kind. Start from another kind."}
              </PanelEmpty>
            </div>
          )}
          {graph.busy && (
            <div
              role="status"
              className="absolute top-3 left-3 flex items-center gap-1.5 rounded-(--radius-chip) border bg-card px-2 py-1 text-2xs text-muted-foreground shadow-(--shadow-raised)"
            >
              <Spinner className="size-3" aria-hidden="true" role={undefined} />
              Loading {graph.busy === "neighbors" ? "relationships" : "nodes"}…
            </div>
          )}
        </div>

        <div
          ref={panelRef}
          className="relative min-h-0 min-w-0 [grid-area:inspector]"
        >
          <InspectorResizer
            panelRef={panelRef}
            current={current}
            max={max}
            resize={resize}
          />
          <aside
            className={`${PANEL} flex h-full flex-col overflow-hidden @max-[1270px]/shell:h-auto`}
            aria-label="Inspector"
          >
            <header className="flex h-10 shrink-0 items-center justify-between border-b pr-1.5 pl-3">
              <h3 className="text-xs font-medium text-foreground">Inspector</h3>
              {selection && (
                <Button
                  variant="ghost"
                  size="icon-xs"
                  aria-label="Clear the selection"
                  onClick={() => setSelection(null)}
                >
                  <Icon icon="close" />
                </Button>
              )}
            </header>
            <div tabIndex={0} className="min-h-0 flex-1 overflow-y-auto p-3">
              {selection ? (
                <Inspector
                  key={`${selection.type}:${selection.id}`}
                  organizationId={organizationId}
                  data={data}
                  graph={graph.graph}
                  selection={selection}
                  onSelect={setSelection}
                  actions={
                    selectedNode && (
                      <div className="flex flex-wrap gap-1.5 *:flex-1">
                        <Button
                          size="sm"
                          disabled={!!graph.busy || atLimit || cursor === null}
                          onClick={() => void graph.expand(selectedNode)}
                        >
                          <Icon icon="plus" data-icon="inline-start" />
                          {cursor === null
                            ? "All relationships loaded"
                            : cursor
                              ? "Load more relationships"
                              : "Expand relationships"}
                        </Button>
                        <Button
                          size="sm"
                          variant="outline"
                          disabled={!!graph.busy}
                          onClick={() => {
                            graph.focus(selectedNode)
                            resetFilters()
                          }}
                        >
                          Focus on this node
                        </Button>
                      </div>
                    )
                  }
                />
              ) : (
                <PanelEmpty icon="branch">
                  Select a node or relationship to see its kind, source and
                  properties. Double-click a node to follow its relationships.
                </PanelEmpty>
              )}
            </div>
          </aside>
        </div>
      </div>
    </div>
  )
}

/**
 * The explorer's toolbar, under the top bar: the page's view tabs, which
 * published generation is on the canvas, and where to start from.
 */
function GraphToolbar({
  tabs,
  data,
  counts,
  loading,
  kind,
  onKindChange,
  onReload,
}: {
  tabs: React.ReactNode
  data: GraphData | null
  /** What's on the canvas of what's loaded. */
  counts: string | null
  loading: boolean
  kind: string
  onKindChange: (kind: string) => void
  onReload: () => void
}) {
  return (
    <ShellToolbar>
      <ViewToolbar>
        {tabs}
        {data && data.generation > 0 ? (
          <p className="flex shrink-0 items-center gap-1.5 text-xs whitespace-nowrap text-muted-foreground">
            <span
              aria-hidden="true"
              className="size-1.5 shrink-0 rounded-full bg-online"
            />
            <span className="font-medium text-foreground">
              Generation {data.generation}
            </span>
            <span aria-hidden="true">·</span>
            <span className="flex min-w-0 items-center gap-1">
              <Icon icon="branch" size={13} />
              <span className="max-w-48 truncate" title={data.branch}>
                {data.branch || "branch not recorded"}
              </span>
            </span>
            <span aria-hidden="true">·</span>
            <code className="font-mono text-2xs" title={data.commit_sha}>
              {data.commit_sha?.slice(0, 12) || "commit not recorded"}
            </code>
          </p>
        ) : loading ? (
          <Skeleton className="h-4 w-60" />
        ) : null}
        {counts && data && data.generation > 0 && (
          <p
            title={counts}
            className="min-w-0 truncate border-l pl-[13px] text-xs text-muted-foreground tabular-nums @max-[800px]/shell:border-l-0 @max-[800px]/shell:pl-0"
          >
            {counts}
          </p>
        )}
        <div className="ml-auto flex items-center gap-[7px]">
          <Select
            items={KIND_ITEMS}
            value={kind}
            onValueChange={(value) => onKindChange(value ?? "")}
          >
            <SelectTrigger className="text-[0.8125rem]">
              <span className="text-muted-foreground">Start from</span>
              <SelectValue />
            </SelectTrigger>
            <SelectContent alignItemWithTrigger={false}>
              {KIND_ITEMS.map((item) => (
                <SelectItem key={item.value || "any"} value={item.value}>
                  {item.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <ToolbarButton icon="refresh" onClick={onReload}>
            Reload
          </ToolbarButton>
        </div>
      </ViewToolbar>
    </ShellToolbar>
  )
}

// Where the inspector's width is kept, per browser.
const INSPECTOR_WIDTH_KEY = "forge:code-graph:inspector-width"
const INSPECTOR_MIN = 288
// The canvas keeps at least this much when the inspector is dragged wide.
const CANVAS_MIN = 400
const INSPECTOR_STEP = 24

function storedWidth() {
  try {
    const value = Number(localStorage.getItem(INSPECTOR_WIDTH_KEY))
    return Number.isFinite(value) && value >= INSPECTOR_MIN ? value : null
  } catch {
    return null
  }
}

/**
 * The inspector's width: the layout's default (a share of the workbench)
 * until it's dragged, then that width, remembered in this browser. It never
 * leaves the canvas less than CANVAS_MIN; a narrower window narrows it for
 * now without forgetting it. Measures once the workbench is on the page
 * (`ready`).
 */
function useInspectorWidth(ready: boolean) {
  const gridRef = React.useRef<HTMLDivElement>(null)
  const panelRef = React.useRef<HTMLDivElement>(null)
  const [chosen, setChosen] = React.useState<number | null>(storedWidth)
  const [measured, setMeasured] = React.useState({
    panel: 0,
    max: Number.POSITIVE_INFINITY,
  })

  React.useEffect(() => {
    const grid = gridRef.current
    const panel = panelRef.current
    if (!ready || !grid || !panel) return
    const measure = () => {
      // The workbench less the finder (16rem), the canvas's minimum and
      // the two gaps.
      const rem = parseFloat(
        getComputedStyle(document.documentElement).fontSize
      )
      setMeasured({
        panel: Math.round(panel.getBoundingClientRect().width),
        max: Math.max(
          INSPECTOR_MIN,
          Math.round(grid.clientWidth - 16 * rem - CANVAS_MIN - 24)
        ),
      })
    }
    const observer = new ResizeObserver(measure)
    observer.observe(grid)
    observer.observe(panel)
    return () => observer.disconnect()
  }, [ready])

  const width = chosen === null ? null : Math.min(chosen, measured.max)
  const resize = React.useCallback(
    (next: number | null, save = true) => {
      const clamped =
        next === null
          ? null
          : Math.round(Math.min(Math.max(next, INSPECTOR_MIN), measured.max))
      setChosen(clamped)
      if (!save) return
      try {
        if (clamped === null) localStorage.removeItem(INSPECTOR_WIDTH_KEY)
        else localStorage.setItem(INSPECTOR_WIDTH_KEY, String(clamped))
      } catch {
        // Not remembered; it still applies until the page is left.
      }
    },
    [measured.max]
  )

  return {
    gridRef,
    panelRef,
    /** What it measures now, dragged or not. */
    current: width ?? measured.panel,
    max: measured.max,
    resize,
    style: (width === null
      ? undefined
      : { "--inspector-width": `${width}px` }) as
      React.CSSProperties | undefined,
  }
}

/**
 * The inspector's left edge, to drag wider or narrower. With the keyboard:
 * arrows step it (Shift for bigger steps), Home and End go to the narrowest
 * and widest, Enter puts it back to the default. Double-clicking also
 * resets it.
 */
function InspectorResizer({
  panelRef,
  current,
  max,
  resize,
}: {
  panelRef: React.RefObject<HTMLDivElement | null>
  current: number
  max: number
  resize: (width: number | null, save?: boolean) => void
}) {
  const [dragging, setDragging] = React.useState(false)

  function widthAt(clientX: number) {
    const right = panelRef.current?.getBoundingClientRect().right ?? 0
    return right - clientX
  }
  return (
    <div
      role="separator"
      aria-orientation="vertical"
      aria-label="Resize the inspector"
      aria-valuenow={current || undefined}
      aria-valuemin={INSPECTOR_MIN}
      aria-valuemax={Number.isFinite(max) ? max : undefined}
      tabIndex={0}
      data-dragging={dragging || undefined}
      title="Drag to resize · double-click to reset"
      className="group/resizer absolute inset-y-0 -left-3 z-10 flex w-3 cursor-col-resize touch-none justify-center outline-none @max-[1270px]/shell:hidden"
      onPointerDown={(e) => {
        if (e.button !== 0) return
        e.preventDefault()
        e.currentTarget.setPointerCapture(e.pointerId)
        setDragging(true)
      }}
      onPointerMove={(e) => {
        if (dragging) resize(widthAt(e.clientX), false)
      }}
      onPointerUp={(e) => {
        if (!dragging) return
        setDragging(false)
        resize(widthAt(e.clientX))
      }}
      onPointerCancel={() => setDragging(false)}
      onDoubleClick={() => resize(null)}
      onKeyDown={(e) => {
        const step = e.shiftKey ? INSPECTOR_STEP * 4 : INSPECTOR_STEP
        const moves: Record<string, number | null> = {
          ArrowLeft: current + step,
          ArrowRight: current - step,
          Home: INSPECTOR_MIN,
          End: max,
          Enter: null,
        }
        if (!(e.key in moves)) return
        e.preventDefault()
        resize(moves[e.key])
      }}
    >
      <span
        aria-hidden="true"
        className="h-full w-0.5 rounded-full bg-transparent transition-colors group-hover/resizer:bg-border group-focus-visible/resizer:bg-ring group-data-dragging/resizer:bg-ring"
      />
    </div>
  )
}

/**
 * Whether a successful ingestion published after the generation being
 * explored: a later generation when it names one, or else another commit.
 */
function isNewer(success: CodeIngestion | null, data: GraphData) {
  if (!success) return false
  if (success.generation) return success.generation > data.generation
  return (
    !!data.commit_sha &&
    !!success.commit_sha &&
    success.commit_sha !== data.commit_sha
  )
}

/**
 * Whether the repository has moved on from the generation being explored:
 * it's ingesting again, or a newer ingestion has published another commit.
 * The session stays on its generation until it's reloaded. The page that
 * holds the explorer rereads its repositories while one ingests, so this
 * follows the ingestion as it goes.
 */
function IngestionNotice({
  repository,
  data,
  onReload,
}: {
  repository: CodeRepository
  data: GraphData
  onReload: () => void
}) {
  const success = repository.last_success
  const newer = isNewer(success, data)
  const ingesting = isActive(repository.latest_ingestion?.status)
  if (!ingesting && !newer) return null
  const commit = shortSha(success?.commit_sha)
  return (
    <div
      role="status"
      className="flex flex-wrap items-center justify-between gap-2 rounded-(--radius-item) bg-notice-surface px-3 py-2 text-xs text-foreground"
    >
      <span className="flex items-center gap-1.5">
        {newer ? (
          <Icon icon="info" size={14} className="text-muted-foreground" />
        ) : (
          <Spinner
            aria-hidden="true"
            role={undefined}
            className="size-3.5 text-signal-running"
          />
        )}
        {newer
          ? commit
            ? `A newer graph is published, from commit ${commit}.`
            : "A newer graph is published."
          : `${repository.name} is ingesting again. This is its last published graph until you reload.`}
      </span>
      {newer && (
        <Button variant="outline" size="xs" onClick={onReload}>
          <Icon icon="refresh" data-icon="inline-start" />
          Reload
        </Button>
      )}
    </div>
  )
}

/**
 * What the canvas's shapes, colours and outlines mean: the families, and
 * the derived and unresolved outlines when any such node is loaded. Each
 * kind's glyph is keyed in the finder's kind filters.
 */
function Legend({ kinds }: { kinds: string[] }) {
  const origins = new Set(kinds.map((kind) => lookOf(kind).origin))
  return (
    <ul aria-label="Legend" className="flex flex-wrap gap-x-3 gap-y-1">
      {FAMILIES.map((family) => (
        <li key={family} className="flex items-center gap-1.5">
          <NodeMark family={family} size={11} />
          {FAMILY_LABELS[family]}
        </li>
      ))}
      {origins.has("derived") && (
        <li className="flex items-center gap-1.5">
          <NodeMark family="other" origin="derived" size={11} />
          Derived by the compiler
        </li>
      )}
      {origins.has("unresolved") && (
        <li className="flex items-center gap-1.5">
          <NodeMark family="other" origin="unresolved" size={11} />
          Unresolved
        </li>
      )}
    </ul>
  )
}

function RecordButton({
  active,
  onClick,
  lead,
  title,
  detail,
}: {
  active: boolean
  onClick: () => void
  lead: React.ReactNode
  title: string
  detail: string
}) {
  return (
    <button
      type="button"
      aria-pressed={active}
      onClick={onClick}
      className="flex w-full min-w-0 items-center gap-2 rounded-(--radius-item) px-1.5 py-1 text-left text-xs hover:bg-muted aria-pressed:bg-accent"
    >
      {lead}
      <span className="flex min-w-0 flex-col">
        <span className="truncate text-foreground">{title}</span>
        <span className="truncate text-2xs text-subtle">{detail}</span>
      </span>
    </button>
  )
}

/** Finds nodes by exact name, to add them to the canvas. */
function SymbolFinder({
  graph,
  organizationId,
  repositoryId,
  generation,
  onSelect,
  disabled,
}: {
  graph: CodeGraphReads
  organizationId: string
  repositoryId: string
  generation: number
  onSelect: (node: NodeVersion) => void
  disabled: boolean
}) {
  const [query, setQuery] = React.useState("")
  const [submitted, setSubmitted] = React.useState("")
  const results = useQuery({
    queryKey: codeGraphKeys.symbols(
      organizationId,
      repositoryId,
      generation,
      submitted
    ),
    queryFn: ({ signal }) => graph.symbols(submitted, generation, signal),
    enabled: submitted !== "",
    // Shown here, not as a toast.
    meta: { silent: true },
  })
  return (
    <section className="flex flex-col gap-2">
      <form
        onSubmit={(e) => {
          e.preventDefault()
          setSubmitted(query.trim())
        }}
      >
        <label
          htmlFor="graph-symbol"
          className="mb-1.5 block text-xs font-medium text-foreground"
        >
          Find a symbol
        </label>
        <InputGroup className="h-7">
          <InputGroupInput
            id="graph-symbol"
            placeholder="Exact name, e.g. parse"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            disabled={disabled}
            className="text-xs"
          />
          <InputGroupAddon align="inline-end">
            <InputGroupButton
              size="icon-xs"
              type="submit"
              aria-label="Find"
              disabled={disabled || !query.trim()}
            >
              {results.isFetching ? <Spinner /> : <Icon icon="search" />}
            </InputGroupButton>
          </InputGroupAddon>
        </InputGroup>
      </form>
      {results.error && (
        <p className="text-2xs text-destructive" role="alert">
          {graphErrorMessage(results.error)}
        </p>
      )}
      {results.data && (
        <div className="flex max-h-40 flex-col overflow-y-auto">
          {results.data.nodes.length ? (
            results.data.nodes.map((v) => (
              <RecordButton
                key={v.fact.node.id}
                active={false}
                onClick={() => {
                  onSelect(v)
                  setSubmitted("")
                }}
                lead={<KindBadge kind={v.fact.node.kind} size={18} />}
                title={nodeLabel(v.fact.node)}
                detail={v.fact.node.qualified_name || v.fact.node.kind}
              />
            ))
          ) : (
            <p className="text-2xs text-subtle">
              Nothing is named exactly that.
            </p>
          )}
        </div>
      )}
    </section>
  )
}

function Inspector({
  organizationId,
  data,
  graph,
  selection,
  onSelect,
  actions,
}: {
  organizationId: string
  data: GraphData
  graph: CodeGraphReads
  selection: NonNullable<Selection>
  onSelect: (selection: Selection) => void
  /** What can be done with a selected node. */
  actions?: React.ReactNode
}) {
  const version =
    selection.type === "node"
      ? data.nodes.find((v) => v.fact.node.id === selection.id)
      : data.edges.find((v) => v.fact.edge.id === selection.id)
  if (!version) return null
  const node = "node" in version.fact ? version.fact.node : null
  const edge = "edge" in version.fact ? version.fact.edge : null
  const record = node ?? edge!
  // A node's source text is under View source; don't repeat it here.
  const properties = Object.entries(record.properties ?? {}).filter(
    ([key]) => !(key === "source_text" && record.source)
  )
  return (
    <div className="flex min-w-0 flex-col gap-3 text-xs">
      <div className="flex flex-col gap-1">
        <span className="flex items-center gap-1.5 text-2xs text-muted-foreground">
          {node && <KindBadge kind={node.kind} size={14} />}
          {selection.type === "node" ? "Node" : "Relationship"} · {record.kind}
        </span>
        <h4 className="text-sm font-medium break-words text-foreground">
          {node ? nodeLabel(node) : record.kind}
        </h4>
        {node?.qualified_name && (
          <p className="font-mono text-2xs break-all text-muted-foreground">
            {node.qualified_name}
          </p>
        )}
      </div>
      {edge && (
        <div className="flex flex-col items-start gap-0.5">
          <Button
            variant="link"
            size="xs"
            className="h-auto p-0"
            onClick={() => onSelect({ type: "node", id: edge.source_id })}
          >
            {nameFor(data, edge.source_id)}
          </Button>
          <span className="text-2xs text-subtle">↓ {edge.kind}</span>
          <Button
            variant="link"
            size="xs"
            className="h-auto p-0"
            onClick={() => onSelect({ type: "node", id: edge.target_id })}
          >
            {nameFor(data, edge.target_id)}
          </Button>
        </div>
      )}
      {actions}
      {node?.source && (
        <NodeSource
          graph={graph}
          organizationId={organizationId}
          repositoryId={data.repository_id}
          node={node.id}
          generation={data.generation}
        />
      )}
      <dl className={FACTS}>
        <Fact label="ID" mono>
          {record.id}
        </Fact>
        <Fact label="Since">Generation {version.gen_from}</Fact>
        <Fact label="Commit" mono>
          {version.commit_from}
        </Fact>
        {record.source && (
          <Fact label="Lines">
            {record.source.span.start.line}–{record.source.span.end.line}
          </Fact>
        )}
      </dl>
      {properties.length > 0 && (
        <section className="flex flex-col gap-2.5 border-t pt-3">
          <h5 className="text-2xs font-medium text-muted-foreground">
            Properties
          </h5>
          <dl className={FACTS}>
            {properties.map(([key, value]) => {
              const text = String(propertyValue(value))
              return (
                <Fact
                  key={key}
                  label={key}
                  mono
                  long={text.length > LONG_PROPERTY}
                  property
                >
                  {text}
                </Fact>
              )
            })}
          </dl>
        </section>
      )}
    </div>
  )
}

// Keys in a column as wide as the longest; values take the rest.
const FACTS = "grid grid-cols-[fit-content(40%)_minmax(0,1fr)] gap-x-4 gap-y-2"

/**
 * One key and value in the inspector. A long value (a docstring, a
 * signature) gets the full width in a scrolling box of its own.
 */
function Fact({
  label,
  mono = false,
  long = false,
  property = false,
  children,
}: {
  label: string
  mono?: boolean
  long?: boolean
  /** A property's key, shown as the graph names it. */
  property?: boolean
  children: React.ReactNode
}) {
  const key = (
    <dt
      className={
        property
          ? "font-mono text-2xs leading-5 break-all text-muted-foreground"
          : "leading-5 text-muted-foreground"
      }
    >
      {label}
    </dt>
  )
  if (long) {
    return (
      <div className="col-span-2 flex min-w-0 flex-col gap-1">
        {key}
        <dd
          tabIndex={0}
          className="max-h-48 overflow-auto rounded-(--radius-item) border bg-muted px-2.5 py-2 font-mono text-2xs leading-relaxed whitespace-pre-wrap text-foreground"
        >
          {children}
        </dd>
      </div>
    )
  }
  return (
    <div className="contents">
      {key}
      <dd
        className={
          mono
            ? "min-w-0 font-mono text-2xs leading-5 break-all text-foreground"
            : "min-w-0 leading-5 break-words text-foreground"
        }
      >
        {children}
      </dd>
    </div>
  )
}

/** The node's source, as ingested, with a few lines around it. */
function NodeSource({
  graph,
  organizationId,
  repositoryId,
  node,
  generation,
}: {
  graph: CodeGraphReads
  organizationId: string
  repositoryId: string
  node: string
  generation: number
}) {
  const [open, setOpen] = React.useState(false)
  const source = useQuery({
    queryKey: codeGraphKeys.source(
      organizationId,
      repositoryId,
      generation,
      node
    ),
    queryFn: ({ signal }) => graph.source(node, generation, signal),
    enabled: open,
    staleTime: Infinity, // a generation's source never changes
    meta: { silent: true },
  })
  return (
    <Collapsible
      open={open}
      onOpenChange={setOpen}
      className="flex flex-col gap-2"
    >
      <CollapsibleTrigger
        render={<Button size="xs" variant="outline" className="self-start" />}
      >
        <Icon icon="code" data-icon="inline-start" />
        {open ? "Hide source" : "View source"}
      </CollapsibleTrigger>
      <CollapsibleContent>
        {source.error ? (
          <p className="text-2xs text-destructive" role="alert">
            {graphErrorMessage(source.error)}
          </p>
        ) : source.data ? (
          <div className="flex flex-col gap-1">
            <p className="text-2xs text-subtle">
              Lines {source.data.text_start_line}–{source.data.text_end_line}
              {source.data.truncated ? ", cut short" : ""}
            </p>
            <pre
              tabIndex={0}
              className="max-h-[28rem] overflow-auto rounded-(--radius-item) border bg-muted px-2.5 py-2 font-mono text-2xs leading-relaxed"
            >
              {source.data.text}
            </pre>
          </div>
        ) : (
          <Skeleton className="h-24 w-full" />
        )}
      </CollapsibleContent>
    </Collapsible>
  )
}

// Longer property values scroll in a box of their own.
const LONG_PROPERTY = 160

function nameFor(data: GraphData, id: string) {
  const node = data.nodes.find((v) => v.fact.node.id === id)?.fact.node
  return node ? nodeLabel(node) : id
}
