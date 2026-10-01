import * as React from "react"

import { CopyButton } from "@/components/events/json-view"
import type { StepData } from "@/lib/workflows/model"
import { useBuilder } from "./builder-store"

/**
 * How later steps read this one. Its ID is the builder's, made unique when
 * the step was added; nobody types or changes it.
 */
function OutputReference({ id, entry }: { id: string; entry: boolean }) {
  const path = entry ? "input" : `steps.${id}.output`
  return (
    <div className="flex flex-col gap-1.5">
      <span className="text-xs font-medium text-foreground">
        {entry ? "Later steps read its fields as" : "Later steps read its output as"}
      </span>
      <div className="flex items-center gap-2 rounded-(--radius-control) border py-1.5 pr-1.5 pl-2.5">
        <code className="min-w-0 flex-1 truncate font-mono text-xs text-foreground">{path}</code>
        <CopyButton
          value={path}
          label="Copy"
          variant="ghost"
          size="xs"
          aria-label={`Copy ${path}`}
          className="text-muted-foreground"
        />
      </div>
    </div>
  )
}

/** What a step can read: the run's input and every step that can come before it. */
function Upstream({ id }: { id: string }) {
  const nodes = useBuilder((s) => s.nodes)
  const edges = useBuilder((s) => s.edges)
  const before = React.useMemo(() => {
    const seen = new Set<string>()
    const queue = [id]
    while (queue.length) {
      const at = queue.shift()!
      for (const e of edges) {
        if (e.target === at && !seen.has(e.source) && e.source !== id) {
          seen.add(e.source)
          queue.push(e.source)
        }
      }
    }
    return nodes.filter((n) => seen.has(n.id) && n.data.kind !== "entry")
  }, [id, nodes, edges])

  const rows = [
    { key: "input", label: "The run's input", path: "input" },
    ...before.map((n) => ({ key: n.id, label: n.data.name, path: `steps.${n.id}.output` })),
  ]
  return (
    <div className="flex flex-col gap-1.5">
      <span className="text-xs font-medium text-foreground">It can read</span>
      <ul className="flex flex-col rounded-(--radius-control) border">
        {rows.map((row) => (
          <li
            key={row.key}
            className="flex items-center gap-2 border-t py-1.5 pr-1.5 pl-2.5 first:border-t-0"
          >
            <span className="flex min-w-0 flex-1 flex-col">
              <span className="truncate text-xs text-foreground">{row.label}</span>
              <code className="truncate font-mono text-2xs text-muted-foreground">
                {row.path}
              </code>
            </span>
            <CopyButton
              value={row.path}
              label="Copy"
              variant="ghost"
              size="xs"
              aria-label={`Copy ${row.path}`}
              className="text-muted-foreground"
            />
          </li>
        ))}
      </ul>
    </div>
  )
}

/** How later steps read the step, and what it can read. */
export function DataSection({ id, step }: { id: string; step: StepData }) {
  return (
    <>
      <OutputReference id={id} entry={step.kind === "entry"} />
      <Upstream id={id} />
    </>
  )
}
