import type { FilesRule } from "@/features/adk-workflows/lib/files"
import { RunDialog } from "@/features/runs/components/run-dialog"
import { RunsMenu } from "@/features/runs/components/runs-menu"
import {
  useAdkWorkflowRuns,
  useRunAdkWorkflow,
  type AdkRun,
} from "@/features/runs/lib/runs"

/*
 * Running a workflow from its builder: the run dialog and runs menu,
 * on the workflow's own runs (runs/lib/runs). Each run opens its page
 * under the workflows page's Runs.
 */

/** The workflow's latest runs, each opening its page. */
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
        label: "All workflow runs",
        search: { view: "agents", agentsTab: "runs" },
      }}
      runSearch={(runId) => ({ view: "agents", agentRun: runId })}
    />
  )
}

/** Running the workflow as it's saved now, with an input for its start. */
export function AdkRunDialog({
  open,
  onOpenChange,
  organizationId,
  agentId,
  name,
  inputSchema,
  files,
  version,
  onStarted,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  organizationId: string
  agentId: string
  name: string
  /** The start's input schema; `{}` declares none. */
  inputSchema: Record<string, unknown>
  /** The files the start takes. */
  files?: FilesRule
  /** Which version runs: a published one, or the draft. */
  version?: number | "draft"
  onStarted: (run: AdkRun) => void
}) {
  // Here rather than in the builder: its state changes redraw only the dialog.
  const run = useRunAdkWorkflow(organizationId, agentId, version)
  return (
    <RunDialog
      open={open}
      onOpenChange={onOpenChange}
      run={run}
      words={{
        description: (
          <>
            {name || "The workflow"} runs{" "}
            {version === "draft" || version === undefined
              ? "as its draft is saved now"
              : `as version ${version}`}
            , as you.
            Follow it under Runs, or on its own page.
          </>
        ),
        refused: "The run can't start",
        forbidden: {
          title: "You can't run the organization's workflows",
          description:
            "Running them takes agents:run in the organization. Ask an organization admin.",
        },
      }}
      inputSchema={inputSchema}
      files={files}
      onStarted={onStarted}
    />
  )
}
