import * as React from "react"

import { Icon } from "@/components/forge/icon"
import { Button } from "@/components/ui/button"
import { CopyButton } from "@/features/json/components/json-view"
import { useCopyToClipboard } from "@/hooks/use-copy-to-clipboard"
import { adkName, type AgentStep } from "@/features/adk-workflows/lib/model"
import { agentTypes } from "@/features/adk-workflows/lib/scope"
import { typeContextOf } from "@/features/adk-workflows/lib/validate"
import { typeLabel, type DataType } from "@/features/steps/lib/types"
import { agentGraphOf, useAgentBuilder } from "./agent-store"

/** A labelled row of the Data section: a value in mono, and a way to copy it. */
function Line({
  label,
  value,
  detail,
}: {
  label: string
  value: string
  detail?: string
}) {
  return (
    <div className="flex flex-col gap-1.5">
      <span className="text-xs font-medium text-foreground">{label}</span>
      <div className="flex items-center gap-2 rounded-(--radius-control) border py-1.5 pr-1.5 pl-2.5">
        <span className="flex min-w-0 flex-1 flex-col">
          <code className="truncate font-mono text-xs text-foreground">
            {value}
          </code>
          {detail && (
            <span className="truncate text-2xs text-muted-foreground">
              {detail}
            </span>
          )}
        </span>
        <CopyButton
          value={value}
          label="Copy"
          variant="ghost"
          size="xs"
          aria-label={`Copy ${value}`}
          className="text-muted-foreground"
        />
      </div>
    </div>
  )
}

/** Copies a path, saying so for a moment. */
function CopyPath({ path }: { path: string }) {
  const { isCopied, copyToClipboard } = useCopyToClipboard({
    copiedDuration: 1500,
  })
  return (
    <Button
      type="button"
      variant="ghost"
      size="icon-xs"
      aria-label={`Copy ${path}`}
      onClick={() => copyToClipboard(path)}
      className="shrink-0 text-muted-foreground opacity-0 group-focus-within/row:opacity-100 group-hover/row:opacity-100"
    >
      <Icon icon={isCopied ? "check" : "copy"} />
    </Button>
  )
}

/** A field's row in a tree: its name, its type, and its path to copy. */
type Row = {
  key: string
  name: string
  path: string
  type: DataType
  depth: number
  /** A list's item, which has no name of its own. */
  each?: boolean
}

// Deeper than this, or more rows than that, and the tree stops.
const TREE_DEPTH = 5
const TREE_ROWS = 80

/**
 * The rows of a type's tree: each field (a list's item's fields read
 * through the list, every item's), depth first, under a root path.
 */
function rowsOf(type: DataType, path: string): Row[] {
  const rows: Row[] = []
  const walk = (type: DataType, path: string, depth: number) => {
    if (depth > TREE_DEPTH) return
    if (type.kind === "array" && type.items.kind === "object") {
      rows.push({
        key: `${path}[]`,
        name: "each item",
        path,
        type: type.items,
        depth,
        each: true,
      })
      walk(type.items, path, depth + 1)
      return
    }
    if (type.kind !== "object") return
    for (const [name, field] of Object.entries(type.properties)) {
      if (rows.length >= TREE_ROWS) return
      if (!name.trim()) continue
      const written = /^[A-Za-z_]\w*$/.test(name) ? name : `\`${name}\``
      const at = `${path}.${written}`
      rows.push({ key: at, name, path: at, type: field, depth })
      walk(field, at, depth + 1)
    }
  }
  walk(type, path, 0)
  return rows
}

/**
 * Data as a tree of its fields, each with its type and its path to copy:
 * what a node is handed, or hands on. The root's own row says its type.
 */
function TypeTree({
  label,
  path,
  type,
}: {
  label: string
  /** How it's read: `previous`, `steps.<id>.output`. */
  path: string
  type: DataType
}) {
  const rows = React.useMemo(() => rowsOf(type, path), [type, path])
  return (
    <div className="flex flex-col gap-1.5">
      <span className="text-xs font-medium text-foreground">{label}</span>
      <div className="flex flex-col rounded-(--radius-control) border py-1">
        <div className="group/row flex items-center gap-2 pr-1 pl-2.5">
          <code className="min-w-0 flex-1 truncate font-mono text-xs text-foreground">
            {path}
          </code>
          <span className="shrink-0 font-mono text-2xs text-muted-foreground">
            {typeLabel(type)}
          </span>
          <CopyPath path={path} />
        </div>
        {type.description && (
          <p className="px-2.5 pb-1 text-2xs/[1.5] text-muted-foreground">
            {type.description}
          </p>
        )}
        {rows.length > 0 && (
          <ul className="flex flex-col border-t pt-1">
            {rows.map((row) => (
              <li
                key={row.key}
                title={row.type.description}
                className="group/row flex items-center gap-2 pr-1"
                style={{ paddingLeft: `${0.625 + (row.depth + 1) * 0.75}rem` }}
              >
                {row.each ? (
                  <span className="min-w-0 flex-1 truncate text-2xs text-muted-foreground">
                    each item
                  </span>
                ) : (
                  <code className="min-w-0 flex-1 truncate font-mono text-xs text-foreground">
                    {row.name}
                  </code>
                )}
                <span className="shrink-0 font-mono text-2xs text-muted-foreground">
                  {typeLabel(row.type)}
                </span>
                {row.each ? (
                  <span aria-hidden className="size-6 shrink-0" />
                ) : (
                  <CopyPath path={row.path} />
                )}
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  )
}

/**
 * How a node's data flows: how later nodes read it (Forge's convention,
 * as in a workflow), the name ADK calls it by, what it hands on to the
 * next node, field by field, and what it reads.
 */
export function AgentDataSection({
  id,
  step,
}: {
  id: string
  step: AgentStep
}) {
  const nodes = useAgentBuilder((s) => s.nodes)
  const edges = useAgentBuilder((s) => s.edges)
  const context = useAgentBuilder((s) => s.context)
  const { previous, items, output, state } = React.useMemo(() => {
    const { scopeOf, outputOf } = agentTypes(
      agentGraphOf(nodes, edges),
      typeContextOf(context)
    )
    const scope = scopeOf(id)
    return {
      previous: scope.roots.get("previous")?.type,
      // The items of the loops around it, as it reads them.
      items: [...scope.roots.values()].filter((root) => root.boost === 4),
      state: scope.roots.get("state")!.type,
      output: outputOf(id),
    }
  }, [nodes, edges, context, id])
  const start = step.kind === "start"
  const name = adkName(step.name)
  const keys = state.kind === "object" ? Object.keys(state.properties) : []

  return (
    <>
      <TypeTree
        label={
          start
            ? "Every node reads the input as"
            : "Later nodes read what it hands on as"
        }
        path={start ? "input" : `steps.${id}.output`}
        type={output}
      />
      {previous && (
        <TypeTree
          label="It reads what came in as"
          path="previous"
          type={previous}
        />
      )}
      {items.map((item) => (
        <TypeTree
          key={item.name}
          label={`It reads ${item.detail}`}
          path={item.name}
          type={item.type}
        />
      ))}
      {!start && name && (
        <Line
          label="ADK calls it"
          value={name}
          detail="Its node's name in the graph and the session's events."
        />
      )}
      {!start && (
        <Line
          label="The state it can read"
          value={
            keys.length ? keys.map((k) => `state.${k}`).join(", ") : "state"
          }
          detail="The run's input, and each sub-agent's answer under its ID."
        />
      )}
    </>
  )
}
