import * as React from "react"

import { CopyButton } from "@/features/json/components/json-view"
import { adkName, type AgentStep } from "@/features/adk-workflows/lib/model"
import { agentTypes } from "@/features/adk-workflows/lib/scope"
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

/** A type as the canvas says it: its label, and what it is when known. */
const said = (type: DataType) => ({
  value: typeLabel(type),
  detail: type.description,
})

/**
 * How a node's data flows: how later nodes read it (Forge's convention,
 * as in a workflow), the name ADK calls it by, what it hands on to the
 * next node, and what it reads.
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
  const { previous, output, state } = React.useMemo(() => {
    const { scopeOf, outputOf } = agentTypes(agentGraphOf(nodes, edges))
    const scope = scopeOf(id)
    return {
      previous: scope.roots.get("previous")?.type,
      state: scope.roots.get("state")!.type,
      output: outputOf(id),
    }
  }, [nodes, edges, id])
  const start = step.kind === "start"
  const name = adkName(step.name)
  const keys = state.kind === "object" ? Object.keys(state.properties) : []

  return (
    <>
      <Line
        label={
          start
            ? "Later nodes read its fields as"
            : "Later nodes read its output as"
        }
        value={start ? "input" : `steps.${id}.output`}
        detail={
          start ? "In an instruction or a message: {{ input }}." : undefined
        }
      />
      {!start && name && (
        <Line
          label="ADK calls it"
          value={name}
          detail="Its node's name in the graph and the session's events."
        />
      )}
      <Line label="It hands on" {...said(output)} />
      {previous && <Line label="It reads as previous" {...said(previous)} />}
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
