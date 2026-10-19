import type { ToolCallMessagePartComponent } from "@assistant-ui/react"

import {
  startedRunsOf,
  submittedRunsOf,
} from "@/features/agents/lib/workflow-runs"
import { WorkflowRun } from "./workflow-run-card"

/**
 * A task agent's `finish_task` call (ADK's, which hands its task back),
 * drawn as the runs of the workflows an intake agent started as it
 * completed (its On complete way): where the person confirmed, before the
 * agent it hands back to replies. The runtime puts them in the call's
 * arguments (`workflows`), each with the call the chat follows it by.
 * Nothing shows for a call that started none: it's the task's plumbing.
 */
export const CompletedRuns: ToolCallMessagePartComponent = ({ args }) => {
  const runs = startedRunsOf(args)
  if (!runs.length) return null
  return (
    <>
      {runs.map((run, index) => (
        <WorkflowRun
          key={run.call_id ?? `${run.workflow}-${index}`}
          name={run.workflow}
          title={run.workflow}
          callId={run.call_id}
          result={run}
          answered
        />
      ))}
    </>
  )
}

/**
 * An agent's `submit_requests` call (the requests it filed to its intakes,
 * used as tools), drawn as the runs of the workflows each submitted request
 * started (its intake's On complete way), named for the request first (a
 * card's title is cut short). Nothing shows while it's submitting, or for
 * requests that started none.
 */
export const SubmittedRuns: ToolCallMessagePartComponent = ({ result }) => {
  const runs = submittedRunsOf(result)
  if (!runs.length) return null
  return (
    <>
      {runs.map((run, index) => (
        <WorkflowRun
          key={run.call_id ?? `${run.workflow}-${index}`}
          name={run.workflow}
          title={run.intake ? `${run.intake} · ${run.workflow}` : run.workflow}
          callId={run.call_id}
          result={run}
          answered
        />
      ))}
    </>
  )
}
