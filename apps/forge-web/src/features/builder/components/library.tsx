import * as React from "react"
import { Link } from "@tanstack/react-router"
import { useReactFlow } from "@xyflow/react"

import {
  NavItem,
  NavSectionHeading,
  SidebarHint,
  SidebarSection,
} from "@/components/forge/workspace-sidebar"
import { ShellSidebarHeader, ShellSidebarTop } from "@/components/forge/shell/index"
import { SearchField } from "@/components/forge/toolbar"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import type { KindInfo } from "@/features/builder/lib/types"
import { KindGlyph } from "./glyph"
import { selectedStep, useBuilder, useBuilderApi, type FlowEdge, type FlowNode } from "./store"
import { useBuilderUi } from "./ui"
import { kindMime, STEP_MIME } from "./utils"

const GAP_X = 88
const GAP_Y = 32
const FALLBACK = { width: 240, height: 64 }

/**
 * The builder's own sub nav, in place of the workspace's: back to the
 * organization's list, then every kind of step, grouped and filterable. Drag
 * one onto the canvas (or onto a connection, to put it in between), or
 * click it (Enter) to add it after the selected step, connected to that
 * step's first way out that goes nowhere yet. Render it anywhere in the
 * page: the top block goes in the shell's pinned `ShellSidebarHeader`, the
 * steps in `ShellSidebarTop`, which scrolls on a short screen.
 */
export function StepLibrary({ organizationId }: { organizationId: string }) {
  const [query, setQuery] = React.useState("")
  const adapter = useBuilder((s) => s.adapter)
  const ui = useBuilderUi()
  const { nouns } = ui
  // The kinds a graph has one of, already on the canvas.
  const placedKinds = useBuilder((s) =>
    s.nodes
      .map((n) => n.data.kind)
      .filter((kind) => s.adapter.unique(kind))
      .sort()
      .join(" ")
  )
  const after = useBuilder((s) => selectedStep(s)?.data.name)
  const name = useBuilder((s) => s.meta.name)
  const steps = useBuilder((s) => s.nodes.length)
  const api = useBuilderApi()
  const readOnly = useBuilder((s) => s.readOnly)
  const flow = useReactFlow<FlowNode, FlowEdge>()

  const words = query.trim().toLowerCase()
  const matches = (kind: string) =>
    !words ||
    `${adapter.kinds[kind].label} ${adapter.kinds[kind].keywords}`.toLowerCase().includes(words)
  const shown = adapter.kindList.filter(matches)
  const placed = new Set(placedKinds.split(" "))

  const add = (kind: string) => {
    const store = api.getState()
    const from = selectedStep(store)
    const canvas = document.querySelector<HTMLElement>(".forge-flow")
    let at: { x: number; y: number }
    let link: { source: string; output: string } | undefined
    // Its ways out that take the kind.
    const outputs = from
      ? adapter
          .outputsOf(from.data)
          .filter((o) => adapter.accepts?.(from.data, o.id, kind) ?? true)
      : []
    if (from && outputs.length && adapter.hasInput(kind)) {
      // After the selected step: off its first way out that goes nowhere.
      const used = new Set(
        store.edges.filter((e) => e.source === from.id).map((e) => e.sourceHandle)
      )
      const open = outputs.find((o) => !used.has(o.id) && !o.fallback) ??
        outputs.find((o) => !used.has(o.id)) ?? outputs[0]
      link = { source: from.id, output: open.id }
      const size = from.measured ?? FALLBACK
      at = {
        x: from.position.x + (size.width ?? FALLBACK.width) + GAP_X,
        y: from.position.y + Math.max(0, outputs.indexOf(open)) * 28,
      }
    } else if (canvas) {
      const box = canvas.getBoundingClientRect()
      const center = flow.screenToFlowPosition({
        x: box.left + box.width / 2,
        y: box.top + box.height / 2,
      })
      at = { x: center.x - FALLBACK.width / 2, y: center.y - FALLBACK.height / 2 }
    } else {
      at = { x: 0, y: 0 }
    }
    at = clear(at, store.nodes)
    store.addStep(kind, { x: Math.round(at.x / 8) * 8, y: Math.round(at.y / 8) * 8 }, link)
    store.setView("canvas")
  }

  return (
    <>
      <ShellSidebarHeader>
        <SidebarSection variant="primary">
          <NavItem
            icon="left"
            render={
              <Link to="/organizations/$organizationId" params={{ organizationId }} search={{ view: ui.listView }} />
            }
          >
            All {nouns.docs}
          </NavItem>
          {/* This one; opens its details panel. */}
          <NavItem
            icon={ui.docIcon}
            active
            meta={steps}
            title={name}
            onClick={() => {
              const store = api.getState()
              store.select(null)
              store.setView("canvas")
              store.setDetails(true)
            }}
          >
            <span className="truncate">{name || `Untitled ${nouns.doc}`}</span>
          </NavItem>
        </SidebarSection>
      </ShellSidebarHeader>
      <ShellSidebarTop>
        <SidebarSection className="pb-3">
          <SearchField
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            aria-label={`Find a ${nouns.step}`}
            placeholder={`Find a ${nouns.step}`}
            shortcut=""
            className="mb-3 w-full @max-[1270px]/shell:w-full @max-[800px]/shell:w-full @max-[600px]/shell:w-full"
          />
          {adapter.groups.map((group) => {
            const kinds = shown.filter((kind) => adapter.kinds[kind].group === group.id)
            if (!kinds.length) return null
            return (
              <div key={group.id} role="group" aria-label={group.label} className="mb-2.5 last:mb-0">
                <NavSectionHeading className="pt-1 pb-1.5">{group.label}</NavSectionHeading>
                {kinds.map((kind) => (
                  <LibraryItem
                    key={kind}
                    kind={kind}
                    info={adapter.kinds[kind]}
                    placed={placed.has(kind)}
                    readOnly={readOnly}
                    onAdd={() => add(kind)}
                  />
                ))}
              </div>
            )
          })}
          {shown.length === 0 && (
            <SidebarHint>
              No {nouns.step} matches “{query.trim()}”.
            </SidebarHint>
          )}
        </SidebarSection>
        <SidebarSection variant="flush" className="pt-3">
          <SidebarHint className="px-1">
            {readOnly ? (
              (ui.readOnlyHint ?? "Read-only") + "."
            ) : (
              <>
            Drag a {nouns.step} onto the canvas, or onto a connection to put it in between.{" "}
            {after ? (
              <>
                Click one to add it after{" "}
                <span className="font-medium text-muted-foreground">{after}</span>.
              </>
            ) : (
              "Click one to add it in the middle of the view."
            )}
              </>
            )}
          </SidebarHint>
        </SidebarSection>
      </ShellSidebarTop>
    </>
  )
}

/** The first spot at or below `at` that no step covers. */
function clear(at: { x: number; y: number }, nodes: FlowNode[]) {
  const spot = { ...at }
  for (let tries = 0; tries < 40; tries += 1) {
    const hit = nodes.find((n) => {
      const w = n.measured?.width ?? FALLBACK.width
      const h = n.measured?.height ?? FALLBACK.height
      return (
        spot.x < n.position.x + w &&
        spot.x + FALLBACK.width > n.position.x &&
        spot.y < n.position.y + h &&
        spot.y + FALLBACK.height > n.position.y
      )
    })
    if (!hit) return spot
    spot.y = hit.position.y + (hit.measured?.height ?? FALLBACK.height) + GAP_Y
  }
  return spot
}

function LibraryItem({
  kind,
  info,
  placed,
  readOnly = false,
  onAdd,
}: {
  kind: string
  info: KindInfo
  /** The one of its kind a graph can have is already on the canvas. */
  placed: boolean
  /** Nothing can be added: the builder is read-only. */
  readOnly?: boolean
  onAdd: () => void
}) {
  const ghost = React.useRef<HTMLDivElement>(null)
  return (
    <>
      <Tooltip>
        <TooltipTrigger
          delay={500}
          render={
            <NavItem
              size="sm"
              draggable={!placed && !readOnly}
              disabled={placed || readOnly}
              meta={placed ? "Added" : undefined}
              onClick={onAdd}
              onDragStart={(event: React.DragEvent<HTMLButtonElement>) => {
                event.dataTransfer.setData(STEP_MIME, kind)
                event.dataTransfer.setData(kindMime(kind), "")
                event.dataTransfer.effectAllowed = "copy"
                if (ghost.current) event.dataTransfer.setDragImage(ghost.current, 20, 28)
              }}
              className="min-h-8 cursor-grab gap-2.5 py-1 text-[0.8125rem] active:cursor-grabbing disabled:cursor-default disabled:opacity-50 disabled:hover:bg-transparent"
            />
          }
        >
          <KindGlyph info={info} size="sm" />
          <span className="truncate">{info.label}</span>
        </TooltipTrigger>
        <TooltipContent side="right" className="max-w-56">
          {info.summary}
        </TooltipContent>
      </Tooltip>
      {/* What the pointer carries while dragging: the step as the canvas draws it. */}
      <div
        ref={ghost}
        aria-hidden="true"
        className="pointer-events-none fixed top-0 -left-[9999px] flex w-[15rem] items-center gap-2.5 rounded-(--radius-card) border bg-background px-3 py-2.5 shadow-(--shadow-float)"
      >
        <KindGlyph info={info} />
        <span className="flex min-w-0 flex-col">
          <span className="truncate text-xs font-medium text-foreground">{info.label}</span>
          <span className="truncate text-2xs text-muted-foreground">{info.summary}</span>
        </span>
      </div>
    </>
  )
}
