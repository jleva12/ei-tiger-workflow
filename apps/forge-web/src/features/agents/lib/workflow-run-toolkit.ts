import { defineToolkit } from "@assistant-ui/react"

import {
  CompletedRuns,
  SubmittedRuns,
} from "@/features/agents/components/completed-runs"
import { WorkflowRunCard } from "@/features/agents/components/workflow-run-card"

/** ADK's call that hands a task back: an intake agent's completing. */
export const FINISH_TASK = "finish_task"

/** An agent's submitting the requests it filed to its intakes (used as tools). */
export const SUBMIT_REQUESTS = "submit_requests"

/**
 * Renderers for the calls of the agent's workflow tools (by the names the
 * model calls them), each drawn as the run it starts (`WorkflowRunCard`),
 * for its intake agents' completing (`finish_task`), drawn as the runs of
 * the workflows they started (`CompletedRuns`), and for its submitting the
 * requests it filed to intakes it uses as tools (`submit_requests`), drawn
 * as theirs (`SubmittedRuns`). The chat provides `WorkflowRunsContext` to
 * follow them.
 */
export const workflowRunToolkit = (names: Readonly<Record<string, string>>) =>
  defineToolkit({
    [FINISH_TASK]: { type: "backend" as const, render: CompletedRuns },
    [SUBMIT_REQUESTS]: { type: "backend" as const, render: SubmittedRuns },
    ...Object.fromEntries(
      Object.keys(names).map((name) => [
        name,
        { type: "backend" as const, render: WorkflowRunCard },
      ])
    ),
  })
