import { defineToolkit } from "@assistant-ui/react"

import { WorkflowRunCard } from "@/features/agents/components/workflow-run-card"

/**
 * Renderers for the calls of the agent's workflow tools (by the names the
 * model calls them), each drawn as the run it starts (`WorkflowRunCard`).
 * The chat provides `WorkflowRunsContext` to follow them.
 */
export const workflowRunToolkit = (names: Readonly<Record<string, string>>) =>
  defineToolkit(
    Object.fromEntries(
      Object.keys(names).map((name) => [
        name,
        { type: "backend" as const, render: WorkflowRunCard },
      ])
    )
  )
