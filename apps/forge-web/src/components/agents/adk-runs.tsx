import { RunDialog } from "@/components/runs/run-dialog"
import { RunsMenu } from "@/components/runs/runs-menu"
import {
  useAdkWorkflowRuns,
  useRunAdkWorkflow,
  type AdkRun,
} from "@/lib/agents/runs"

/*
 * Running an ADK workflow from its builder: the run dialog and runs menu,
 * on the ADK workflow's own runs (lib/agents/runs). Each run opens its page
 * under the ADK workflows page's Runs.
 */

/** The ADK workflow's latest runs, each opening its page. */
export function AdkRunsMenu({
  organizationId,
  agentId,
}: {
  organizationId: string
  agentId: string
}) {
  const runs = useAdkWorkflowRuns(organizationId, agentId)
  return (
    <RunsMenu
      organizationId={organizationId}
      runs={runs}
      all={{
        label: "All ADK workflow runs",
        search: { view: "agents", agentsTab: "runs" },
      }}
      runSearch={(runId) => ({ view: "agents", agentRun: runId })}
    />
  )
}

/** Running the ADK workflow as it's saved now, with an input for its start. */
export function AdkRunDialog({
  open,
  onOpenChange,
  organizationId,
  agentId,
  name,
  inputSchema,
  onStarted,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  organizationId: string
  agentId: string
  name: string
  /** The start's input schema; `{}` declares none. */
  inputSchema: Record<string, unknown>
  onStarted: (run: AdkRun) => void
}) {
  // Here rather than in the builder: its state changes redraw only the dialog.
  const run = useRunAdkWorkflow(organizationId, agentId)
  return (
    <RunDialog
      open={open}
      onOpenChange={onOpenChange}
      run={run}
      words={{
        description: (
          <>
            {name || "The ADK workflow"} runs as it&apos;s saved now, as you.
            Follow it under Runs, or on its own page.
          </>
        ),
        refused: "The run can't start",
        forbidden: {
          title: "You can't run the organization's ADK workflows",
          description:
            "Running them takes agents:run in the organization. Ask an organization admin.",
        },
      }}
      inputSchema={inputSchema}
      onStarted={onStarted}
    />
  )
}
