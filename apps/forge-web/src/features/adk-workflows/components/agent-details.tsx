import * as React from "react"
import { cn } from "cn"

import { Icon } from "@/components/forge/icon"
import { Field, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { Kbd } from "@/components/ui/kbd"
import { Textarea } from "@/components/ui/textarea"
import { countAgents } from "@/features/adk-workflows/lib/model"
import { formatRelative } from "@/lib/format"
import { useAgentBuilder, useAgentBuilderApi } from "./agent-store"
import { IssueList } from "@/features/builder/components/issue-list"
import { useOpenStep } from "@/features/builder/components/utils"

function Section({
  title,
  className,
  children,
}: {
  title?: string
  className?: string
  children: React.ReactNode
}) {
  return (
    <section
      className={cn(
        "flex flex-col gap-4 border-t px-4 py-4 first:border-t-0",
        className
      )}
    >
      {title && (
        <h3 className="-mb-1 text-xs font-medium text-foreground">{title}</h3>
      )}
      {children}
    </section>
  )
}

const KEYS: [keys: string[], action: string][] = [
  [["Click"], "Open a node's settings"],
  [["↵"], "Open the focused node"],
  [["Esc"], "Close its settings"],
  [["Scroll"], "Zoom in and out"],
  [["Drag"], "Move around the canvas"],
  [["⇧", "drag"], "Select several nodes"],
  [["Delete"], "Remove what's selected"],
  [["⌘", "D"], "Duplicate the node"],
  [["⌘", "Z"], "Undo; add ⇧ to redo"],
]

/**
 * The right side of the builder: the agent itself (its name and
 * description, what's in it, what needs fixing, the canvas's keys). Nodes
 * are set up in their own dialog, opened from the canvas.
 */
export function AgentDetails() {
  const meta = useAgentBuilder((s) => s.meta)
  const nodes = useAgentBuilder((s) => s.nodes.length)
  const edges = useAgentBuilder((s) => s.edges.length)
  // Its ADK agents: the agent nodes, and every sub-agent in them.
  const agents = useAgentBuilder((s) => countAgents(s.nodes.map((n) => n.data)))
  const issues = useAgentBuilder((s) => s.issues)
  const api = useAgentBuilderApi()
  const openStep = useOpenStep()
  const nameId = React.useId()
  const descriptionId = React.useId()
  const agentIssues = issues.filter((i) => !i.step)
  const nodeIssues = issues.filter((i) => i.step)

  return (
    <div className="flex flex-col">
      <header className="border-b px-4 pt-4 pb-3.5">
        <h2 className="truncate text-sm font-medium text-foreground">
          {meta.name || "Untitled workflow"}
        </h2>
        <p className="text-2xs text-muted-foreground">
          Google ADK graph · click a node to set it up
        </p>
      </header>
      <Section>
        <Field>
          <FieldLabel htmlFor={nameId}>Name</FieldLabel>
          <Input
            id={nameId}
            value={meta.name}
            onChange={(event) =>
              api.getState().updateMeta({ name: event.target.value }, "name")
            }
          />
        </Field>
        <Field>
          <FieldLabel htmlFor={descriptionId}>Description</FieldLabel>
          <Textarea
            id={descriptionId}
            value={meta.description}
            placeholder="What it's for, and who talks to it."
            onChange={(event) =>
              api
                .getState()
                .updateMeta({ description: event.target.value }, "description")
            }
          />
        </Field>
      </Section>
      <Section title="In it">
        <dl className="flex flex-col text-xs">
          {[
            ["Nodes", nodes.toLocaleString()],
            ["Edges", edges.toLocaleString()],
            ["ADK agents", agents.toLocaleString()],
            ["Changed", formatRelative(meta.updated_at)],
          ].map(([label, value]) => (
            <div
              key={label}
              className="flex items-baseline justify-between gap-4 border-t py-1.5 first:border-t-0"
            >
              <dt className="text-muted-foreground">{label}</dt>
              <dd className="font-medium text-foreground tabular-nums">
                {value}
              </dd>
            </div>
          ))}
        </dl>
      </Section>
      <Section title="To fix" className="gap-2.5">
        {issues.length === 0 ? (
          <p className="flex items-center gap-2 text-xs text-muted-foreground">
            <Icon icon="completed" size={14} className="text-signal-success" />
            Nothing. Every node is set up and reached.
          </p>
        ) : (
          <>
            {agentIssues.length > 0 && <IssueList issues={agentIssues} />}
            {nodeIssues.length > 0 && (
              <IssueList issues={nodeIssues} onOpen={openStep} />
            )}
          </>
        )}
      </Section>
      <Section title="Keys" className="gap-2">
        <ul className="flex flex-col gap-1.5 text-xs text-muted-foreground">
          {KEYS.map(([keys, action]) => (
            <li
              key={action}
              className="flex items-center justify-between gap-3"
            >
              <span>{action}</span>
              <span className="flex shrink-0 gap-1">
                {keys.map((key) => (
                  <Kbd key={key}>{key}</Kbd>
                ))}
              </span>
            </li>
          ))}
        </ul>
      </Section>
    </div>
  )
}
