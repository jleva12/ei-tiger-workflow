import * as React from "react"
import { BaseEdge, EdgeLabelRenderer, getSmoothStepPath, type EdgeProps } from "@xyflow/react"
import { cn } from "cn"

import { Icon } from "@/components/forge/icon"
import { drawnLength, pathOf } from "@/lib/builder/routing"
import type { Point } from "@/lib/builder/types"
import { useEdgeLook } from "./edge-looks"
import { useBuilder, type FlowEdge } from "./store"

/** A loop's way back is dashed: 5 on, 4 off. */
const DASH = [5, 4]

/** An arrowhead at the end of a line, pointing the way it last ran. */
function arrowOf(points: Point[]) {
  const tip = points.at(-1)!
  const from = points.at(-2)!
  const length = Math.hypot(tip.x - from.x, tip.y - from.y) || 1
  const dx = (tip.x - from.x) / length
  const dy = (tip.y - from.y) / length
  const base = { x: tip.x - dx * 7, y: tip.y - dy * 7 }
  return `M ${tip.x} ${tip.y} L ${base.x - dy * 3.5} ${base.y + dx * 3.5} L ${base.x + dy * 3.5} ${base.y - dx * 3.5} Z`
}

/**
 * A connection: a line stepped at right angles with soft corners, routed
 * round the steps, with an arrowhead where it arrives. Its colour says
 * what it means (see lib/builder/connections); a loop's way back is
 * dashed, runs over the loop's body and comes down into the loop. Long
 * branches and a loop's lines carry a label. Hovering a step or a line
 * brings its lines forward and fades the rest; selected, it names the
 * branch beside a button to remove it, and when a step dragged from the
 * library is over it, says the step will go in between.
 */
export const StepEdgeView = React.memo(function StepEdgeView({
  id,
  source,
  target,
  sourceX,
  sourceY,
  targetX,
  targetY,
  sourcePosition,
  targetPosition,
  selected,
  data,
}: EdgeProps<FlowEdge>) {
  const dropping = useBuilder((s) => s.dropEdge === id)
  const removeEdge = useBuilder((s) => s.removeEdge)
  const focus = useBuilder((s) => s.focus)
  const look = useEdgeLook(id)
  const route = look?.route

  const drawn = React.useMemo(() => {
    if (route) {
      // Dashes are counted back from the arrow, so the ways back of one
      // loop dash in step where they share the lane.
      const period = DASH[0] + DASH[1]
      return {
        path: pathOf(route.points),
        arrow: arrowOf(route.points),
        anchor: route.anchor,
        dashOffset: (period - (drawnLength(route.points) % period)) % period,
      }
    }
    // Until both steps are measured: a plain step line.
    const [path, x, y] = getSmoothStepPath({
      sourceX,
      sourceY,
      targetX,
      targetY,
      sourcePosition,
      targetPosition,
      borderRadius: 8,
      offset: 20,
    })
    return {
      path,
      arrow: arrowOf([
        { x: targetX - 8, y: targetY },
        { x: targetX, y: targetY },
      ]),
      anchor: { x, y },
      dashOffset: 0,
    }
  }, [route, sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition])

  const related = focus
    ? "edge" in focus
      ? focus.edge === id
      : focus.step === source || focus.step === target
    : false
  const faded = Boolean(focus) && !related && !selected && !dropping
  const strong = related || selected || dropping
  const tone = look?.tone ?? "neutral"
  const back = look?.role === "return"
  // What it's called: its branch, or a loop's way in or back.
  const named = look?.label ?? (data?.branch ? data.label : undefined)
  const showLabel = Boolean(
    named && route?.label && (route.labelShown || related) && !selected && !dropping
  )

  return (
    <>
      <g
        className={cn(
          "forge-edge-group",
          `forge-tone-${tone}`,
          faded && "forge-edge-faded",
          strong && "forge-edge-strong"
        )}
      >
        <BaseEdge
          id={id}
          path={drawn.path}
          interactionWidth={22}
          className={cn(
            "forge-edge",
            selected && "forge-edge-selected",
            dropping && "forge-edge-drop"
          )}
          style={
            back
              ? {
                  strokeDasharray: DASH.join(" "),
                  strokeDashoffset: drawn.dashOffset,
                }
              : undefined
          }
        />
        <path d={drawn.arrow} className="forge-edge-arrow" aria-hidden="true" />
      </g>
      {showLabel && route?.label && (
        <EdgeLabelRenderer>
          <div
            className={cn(
              "forge-edge-label pointer-events-none absolute",
              `forge-tone-${tone}`,
              faded && "forge-edge-faded"
            )}
            style={{
              transform: `translate(-50%, -50%) translate(${route.label.x}px, ${route.label.y}px)`,
            }}
          >
            {back && <Icon icon="refresh" size={10} className="shrink-0" />}
            <span className="truncate">{named}</span>
          </div>
        </EdgeLabelRenderer>
      )}
      {(selected || dropping) && (
        <EdgeLabelRenderer>
          <div
            className="nodrag nopan pointer-events-auto absolute"
            style={{
              transform: `translate(-50%, -50%) translate(${drawn.anchor.x}px, ${drawn.anchor.y}px)`,
            }}
          >
            {dropping ? (
              <span
                className="flex h-6 items-center gap-1 rounded-(--radius-chip) border border-foreground/30 bg-background px-1.5 text-2xs font-medium whitespace-nowrap text-foreground shadow-(--shadow-raised)"
                aria-label="Insert here"
              >
                <Icon icon="plus" size={12} />
                Insert here
              </span>
            ) : (
              <span className="flex h-6 items-center rounded-(--radius-soft) border bg-background shadow-(--shadow-raised)">
                {named && (
                  <span className="max-w-40 min-w-0 truncate border-r px-2 text-2xs font-medium text-foreground">
                    {named}
                  </span>
                )}
                <button
                  type="button"
                  aria-label="Remove this connection"
                  onClick={() => removeEdge(id)}
                  className="grid h-full w-6 shrink-0 place-items-center rounded-(--radius-soft) text-muted-foreground transition-colors duration-150 hover:bg-danger-surface hover:text-destructive"
                >
                  <Icon icon="close" size={12} />
                </button>
              </span>
            )}
          </div>
        </EdgeLabelRenderer>
      )}
    </>
  )
})
