import * as React from "react"
import { LeftToRightListNumberIcon } from "@hugeicons/core-free-icons"
import { cn } from "cn"

import { KindGlyph } from "@/features/builder/components/glyph"
import { EventItem, EventList } from "@/components/forge/activity"
import { PageEmpty, PanelEmpty } from "@/components/forge/empty-state"
import { ErrorCallout } from "@/components/forge/feedback"
import { Icon } from "@/components/forge/icon"
import { useShellPage } from "@/components/forge/shell/index"
import { Chip, StatusBadge } from "@/components/forge/status"
import {
  AsideFact,
  AsideSection,
  Disclosure,
  ExecutionPlan,
  ExecutionStep,
  LiveLabel,
  PageSection,
  RecordList,
  RecordRow,
  SubmissionLayout,
  TaskMeta,
  TaskPage,
  TaskPageEyebrow,
  TaskPageHeader,
  TaskTabBar,
  TaskTabsList,
  TaskTabsTrigger,
  TaskTitleRow,
  WaitingNotice,
} from "@/components/forge/task-page"
import { CopyButton, JsonView } from "@/features/json/components/json-view"
import {
  ApprovalPanel,
  BoardChoiceRow,
  DecisionBoardPanel,
  HumanInputPanel,
} from "@/features/runs/components/pause-panels"
import { AdkRunTraceTab } from "@/features/runs/components/run-trace"
import {
  RunFileActions,
  RunFileIcon,
  RunFileName,
  RunFileViewer,
} from "@/features/runs/components/run-files"
import {
  inputFileRef,
  runFileKey,
  savedFileRef,
  savedVersionDetail,
  useRunFileDownload,
  type RunFileRef,
} from "@/features/runs/lib/run-files"
import { preview } from "@/features/runs/lib/trace"
import { ValueTree } from "@/features/json/components/value-tree"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Skeleton } from "@/components/ui/skeleton"
import { Spinner } from "@/components/ui/spinner"
import { Tabs, TabsContent } from "@/components/ui/tabs"
import { toast } from "@/components/ui/toast"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import type { MyOrganization } from "@/lib/access"
import { useScopeAccess } from "@/lib/hierarchy"
import type { AgentDocument } from "@/features/adk-workflows/lib/document"
import { useNodeCatalogs } from "@/features/adk-workflows/lib/api"
import { kindInfo } from "@/features/adk-workflows/lib/catalog"
import {
  adkApprovePermission,
  calledWithTitle,
  callRunId,
  eventTitle,
  failureCategory,
  failureTitle,
  humanize,
  isAdkRunTab,
  isFailureEvent,
  gateGroups,
  openGatesOf,
  pauseKindOf,
  responseSchemaOf,
  shortId,
  stepErrorOf,
  stepKindOf,
  stepNameOf,
  stepStatusOf,
  stepTiming,
  triggerLabel,
  useAbandonAdkRun,
  useAdkRun,
  useAdkRunSessionFiles,
  useAdkRunSteps,
  useAnswerAdkRun,
  useDecideAdkRun,
  useResubmitAdkRun,
  useRetryAdkRun,
  useRunDocument,
  versionLabel,
  type AdkRunDetail,
  type AdkRunFile,
  type AdkRunSessionFile,
  type AdkRunPause,
  type AdkRunStep,
  type AdkRunTab,
  type AdkStateChange,
  type AdkToolCall,
} from "@/features/runs/lib/runs"
import type { ChipTone } from "@/components/forge/variants"
import {
  formatBytes,
  formatDateTime,
  formatDuration,
  formatRelative,
} from "@/lib/format"
import { WORKSPACE_VIEWS } from "@/features/organizations/lib/organization-workspace"
import { usePageContext } from "@/features/assistant/lib/page-context"
import {
  isA2aTask,
  isLiveRun,
  isOpenRun,
  runStatusDisplay,
} from "@/features/runs/lib/display"

// A tab's body, on the same gutters as the header and tabs above it.
const BODY =
  "w-full max-w-[1100px] px-[30px] pt-7 pb-[60px] @max-[900px]/shell:px-5 @max-[900px]/shell:py-6 @max-[600px]/shell:px-[15px] @max-[600px]/shell:py-5"

// The Trace tab's body: wider, for its events beside their inspector.
const TRACE_BODY =
  "w-full max-w-[1500px] px-[30px] pt-7 pb-[60px] @max-[900px]/shell:px-5 @max-[900px]/shell:py-6 @max-[600px]/shell:px-[15px] @max-[600px]/shell:py-5"

/**
 * The shell wraps pages in its padded, scrolling `PageContent`; a run's page
 * is its own scroll container, edge to edge, so it fills that region.
 */
function FullBleed({ children }: { children: React.ReactNode }) {
  return <div className="absolute inset-0 flex flex-col">{children}</div>
}

function copyLink() {
  void navigator.clipboard.writeText(window.location.href).then(
    () => toast.add({ title: "Link copied.", type: "info" }),
    () => toast.add({ title: "Couldn't copy the link", type: "error" })
  )
}

// "Primary stepped down" reads on into the next sentence without "..".
const sentence = (text: string) => text.trim().replace(/[.\s]+$/, "")

/* -------------------------------------------------------------------------- */
/* What it waits for                                                          */
/* -------------------------------------------------------------------------- */

type Pause = { organizationId: string; runId: string; pause: AdkRunPause }

/** An approval node's question, decided through the run. */
function AdkApproval({ organizationId, runId, pause }: Pause) {
  // The panel shows the failure itself.
  const decide = useDecideAdkRun(organizationId, runId, {
    meta: { silent: true },
  })
  return (
    <ApprovalPanel
      organizationId={organizationId}
      pause={pause}
      decide={decide}
      permission={adkApprovePermission(pause.details.approvers ?? "org:admin")}
      showDeadline
    />
  )
}

/** A human input node's question, answered through the run. */
function AdkHumanInput({ organizationId, runId, pause }: Pause) {
  // The panel shows the failure itself, a 422's why by the answer.
  const answer = useAnswerAdkRun(organizationId, runId, {
    meta: { silent: true },
  })
  return (
    <HumanInputPanel
      organizationId={organizationId}
      pause={pause}
      responseSchema={responseSchemaOf(pause)}
      answer={answer}
      permission="agents:run"
    />
  )
}

/** What a paused run waits for: a decision, or a person's answer. */
function AdkPausePanel(props: Pause) {
  return pauseKindOf(props.pause) === "human_input" ? (
    <AdkHumanInput {...props} />
  ) : (
    <AdkApproval {...props} />
  )
}

/** One of a Decision board's open choices, decided through the run. */
function AdkBoardChoice({
  organizationId,
  runId,
  pause,
  allowed,
}: Pause & { allowed: boolean }) {
  // The row shows the failure itself.
  const decide = useDecideAdkRun(organizationId, runId, {
    meta: { silent: true },
    said: (approved) => ({
      title: approved ? "Accepted." : "Rejected.",
      description: `${pause.details.board?.label ?? "The choice"} takes its ${approved ? "Accepted" : "Rejected"} way.`,
    }),
  })
  return <BoardChoiceRow pause={pause} decide={decide} allowed={allowed} />
}

/** A Decision board's open choices, each decided on its own. */
function AdkBoard({
  organizationId,
  runId,
  gates,
}: {
  organizationId: string
  runId: string
  gates: AdkRunPause[]
}) {
  const can = useScopeAccess(`org:${organizationId}`)
  const permission = adkApprovePermission(
    gates[0]?.details.approvers ?? "org:admin"
  )
  return (
    <DecisionBoardPanel
      organizationId={organizationId}
      gates={gates}
      permission={permission}
    >
      {gates.map((gate) => (
        <AdkBoardChoice
          key={gate.id}
          organizationId={organizationId}
          runId={runId}
          pause={gate}
          allowed={can(permission)}
        />
      ))}
    </DecisionBoardPanel>
  )
}

/**
 * Every approval and question a run waits at, each decided on its own: a
 * board's choices together, the rest each in a panel of its own. A run may
 * wait at them while it's paused, waiting for a time, or carrying on with
 * a decision made at another.
 */
function AdkGates({
  organizationId,
  run,
}: {
  organizationId: string
  run: AdkRunDetail
}) {
  return (
    <>
      {gateGroups(openGatesOf(run)).map((group) =>
        group.kind === "board" ? (
          <AdkBoard
            key={`board-${group.step}`}
            organizationId={organizationId}
            runId={run.id}
            gates={group.gates}
          />
        ) : (
          <AdkPausePanel
            key={group.gate.id}
            organizationId={organizationId}
            runId={run.id}
            pause={group.gate}
          />
        )
      )}
    </>
  )
}

/**
 * Where the run stands, when that needs saying: what it waits for (a
 * person, a time, a worker), its being stopped, the hiccup it's retried
 * after, or why it failed.
 */
export function StatusNotice({
  organizationId,
  run,
}: {
  organizationId: string
  run: AdkRunDetail
}) {
  return (
    <>
      {openGatesOf(run).length > 0 && (
        <AdkGates organizationId={organizationId} run={run} />
      )}
      <StatusLine run={run} />
    </>
  )
}

/** Where the run stands besides what people decide: a time, a stop, a hiccup, a failure. */
function StatusLine({ run }: { run: AdkRunDetail }) {
  if (run.status === "waiting" && run.waiting_until) {
    return (
      <WaitingNotice
        title={`Carries on ${formatRelative(run.waiting_until)}`}
        state="Waiting"
      >
        {run.waiting_reason ? `${sentence(run.waiting_reason)}. ` : ""}It
        carries on by itself at {formatDateTime(run.waiting_until)}. No worker
        is busy with it meanwhile.
      </WaitingNotice>
    )
  }
  if (run.status === "running" && run.cancel_requested_at) {
    return (
      <WaitingNotice icon="stop" title="Stopping" state="Abandoning">
        Abandoned {formatRelative(run.cancel_requested_at)} while it ran. Its
        worker stops it at its next step, or within a minute, and it&apos;s
        abandoned then.
      </WaitingNotice>
    )
  }
  const error = run.error
  if (error && isLiveRun(run)) {
    const category = failureCategory(error.category)
    return (
      <WaitingNotice
        icon="refresh"
        title="Running again after a hiccup"
        state={category.label}
      >
        The last one: {sentence(error.message)}. {category.description}
        {run.status === "queued" && run.next_attempt_at
          ? ` Next attempt: ${formatDateTime(run.next_attempt_at)}.`
          : ""}
      </WaitingNotice>
    )
  }
  if (run.status === "queued") {
    return (
      <WaitingNotice title="Waiting for a worker" state="Queued">
        It&apos;s queued, and a worker takes it within seconds.
      </WaitingNotice>
    )
  }
  // An A2A task's run isn't retried here: its task ended for good when it failed.
  const a2a = run.status === "failed" && isA2aTask(run) && (
    <span>
      Started over A2A, it isn&apos;t retried here: its A2A task ended for good
      when it failed. Resubmit it to run it again.
    </span>
  )
  if (error && (run.status === "failed" || run.status === "abandoned")) {
    const category = failureCategory(error.category)
    return (
      <ErrorCallout
        title={
          run.status === "failed"
            ? "The run failed"
            : "The run was abandoned after failing"
        }
        className="mb-[34px]"
      >
        <span className="flex flex-col gap-1">
          <span>{error.message}</span>
          <span>
            <strong className="font-medium">{category.label}.</strong>{" "}
            {category.description}
          </span>
          {a2a}
        </span>
      </ErrorCallout>
    )
  }
  if (run.status === "failed") {
    return (
      <ErrorCallout title="The run failed" className="mb-[34px]">
        <span className="flex flex-col gap-1">
          <span>No reason was recorded.</span>
          {a2a}
        </span>
      </ErrorCallout>
    )
  }
  return null
}

/* -------------------------------------------------------------------------- */
/* Overview                                                                   */
/* -------------------------------------------------------------------------- */

const json = (value: unknown) => JSON.stringify(value ?? null, null, 2)

/** One way of a run that ended, when more than one did: the step it ended at, how, and with what. */
type Ending = {
  step: string
  name: string
  outcome: "succeeded" | "failed"
  result: unknown
}

const ENDING_KEYS = ["name", "outcome", "result", "step"]

/**
 * A run's result as its endings, when more than one way ended (ways run at
 * once: a Match taking every rule that holds, a step leading to several);
 * else null, and the result is the one End's.
 */
function endingsOf(result: unknown): Ending[] | null {
  if (!Array.isArray(result) || result.length < 2) return null
  const every = result.every(
    (item) =>
      typeof item === "object" &&
      item !== null &&
      Object.keys(item).sort().join() === ENDING_KEYS.join() &&
      typeof item.step === "string" &&
      typeof item.name === "string" &&
      (item.outcome === "succeeded" || item.outcome === "failed")
  )
  return every ? (result as Ending[]) : null
}

/** Each way that ended: its step, whether it succeeded, and its result. */
function Endings({ endings }: { endings: Ending[] }) {
  return (
    <div className="flex flex-col gap-2">
      {endings.map((ending, index) => (
        <div
          key={`${ending.step}-${index}`}
          className="flex flex-col gap-1.5 rounded-(--radius-control) border px-3 py-2"
        >
          <div className="flex min-w-0 items-center gap-2">
            <span className="min-w-0 truncate text-sm font-medium text-foreground">
              {ending.name || ending.step}
            </span>
            <code className="shrink-0 text-xs text-muted-foreground">
              {ending.step}
            </code>
            <Chip
              tone={ending.outcome === "failed" ? "danger" : "success"}
              className="ml-auto shrink-0"
            >
              {ending.outcome === "failed" ? "Failed" : "Succeeded"}
            </Chip>
          </div>
          <JsonView value={ending.result ?? null} className="max-h-[16rem]" />
        </div>
      ))}
    </div>
  )
}

/**
 * The files a run's agents saved (`save_file`), each with who saved it and
 * when; its latest version previews and downloads, and its earlier ones
 * beneath it.
 */
function SavedFiles({
  organizationId,
  runId,
  files,
  doc,
  onPreview,
}: {
  organizationId: string
  runId: string
  files: AdkRunSessionFile[]
  doc: AgentDocument | undefined
  onPreview: (file: RunFileRef) => void
}) {
  const { download, fetching } = useRunFileDownload(organizationId, runId)
  return (
    <ul className="flex flex-col divide-y divide-border rounded-(--radius-control) border">
      {files.map((file) => {
        const latest = file.versions.at(-1)
        const earlier = file.versions.slice(0, -1).reverse()
        const shown = latest && savedFileRef(file, latest, doc)
        return (
          <li key={file.name} className="flex flex-col gap-1 px-3 py-2">
            <div className="flex min-w-0 items-center gap-2.5">
              <RunFileIcon name={file.name} />
              {shown ? (
                <RunFileName file={shown} onPreview={() => onPreview(shown)} />
              ) : (
                <span className="min-w-0 flex-1 truncate text-sm">
                  {file.name}
                </span>
              )}
              {file.versions.length > 1 && (
                <Chip tone="outline" className="shrink-0">
                  {file.versions.length} versions
                </Chip>
              )}
              <span className="shrink-0 text-xs text-muted-foreground tabular-nums">
                {[file.media_type, latest && savedVersionDetail(latest, doc)]
                  .filter(Boolean)
                  .join(" · ")}
              </span>
              {shown && (
                <RunFileActions
                  file={shown}
                  fetching={fetching === runFileKey(shown)}
                  onPreview={() => onPreview(shown)}
                  onDownload={() => void download(shown)}
                />
              )}
            </div>
            {earlier.length > 0 && (
              <Disclosure summary="Earlier versions" className="pl-7 text-xs">
                <ul className="flex flex-col gap-1">
                  {earlier.map((version) => {
                    const ref = savedFileRef(file, version, doc)
                    return (
                      <li
                        key={version.version}
                        className="flex items-center gap-2 text-xs text-muted-foreground"
                      >
                        <span className="text-foreground">
                          Version {version.version + 1}
                        </span>
                        <span className="min-w-0 flex-1 truncate">
                          {savedVersionDetail(version, doc)}
                        </span>
                        <RunFileActions
                          file={ref}
                          fetching={fetching === runFileKey(ref)}
                          onPreview={() => onPreview(ref)}
                          onDownload={() => void download(ref)}
                        />
                      </li>
                    )
                  })}
                </ul>
              </Disclosure>
            )}
          </li>
        )
      })}
    </ul>
  )
}

/** The files a run started with, each previewed or downloaded as it was sent. */
function RunFiles({
  organizationId,
  runId,
  files,
  onPreview,
}: {
  organizationId: string
  runId: string
  files: AdkRunFile[]
  onPreview: (file: RunFileRef) => void
}) {
  const { download, fetching } = useRunFileDownload(organizationId, runId)
  return (
    <ul className="flex flex-col divide-y divide-border rounded-(--radius-control) border">
      {files.map((file) => {
        const ref = inputFileRef(file)
        return (
          <li
            key={file.name}
            className="flex min-w-0 items-center gap-2.5 px-3 py-2"
          >
            <RunFileIcon name={file.name} />
            <RunFileName file={ref} onPreview={() => onPreview(ref)} />
            <span className="shrink-0 text-xs text-muted-foreground tabular-nums">
              {file.media_type} · {formatBytes(file.size_bytes)}
            </span>
            <RunFileActions
              file={ref}
              fetching={fetching === runFileKey(ref)}
              onPreview={() => onPreview(ref)}
              onDownload={() => void download(ref)}
            />
          </li>
        )
      })}
    </ul>
  )
}

/**
 * The Overview tab: what the run waits for or why it failed, what it was
 * given (its input and files) and what it handed on, and its record, with
 * its timing beside them.
 */
function AdkRunOverview({
  organizationId,
  run,
  doc,
  onOpenRun,
}: {
  organizationId: string
  run: AdkRunDetail
  doc: AgentDocument | undefined
  onOpenRun: (runId: string) => void
}) {
  const sessionFiles = useAdkRunSessionFiles(organizationId, run)
  const saved = React.useMemo(
    () =>
      sessionFiles.data?.files.filter((file) => file.origin === "run") ?? [],
    [sessionFiles.data]
  )
  // Every file it has, to page through in the viewer: what it started
  // with, then what it saved (each one's latest).
  const [previewing, setPreviewing] = React.useState<RunFileRef>()
  const previewable = React.useMemo(
    () => [
      ...(run.files ?? []).map(inputFileRef),
      ...saved.flatMap((file) => {
        const latest = file.versions.at(-1)
        return latest ? [savedFileRef(file, latest, doc)] : []
      }),
    ],
    [run.files, saved, doc]
  )
  const ended = run.finished_at !== null
  const hasResult = run.result !== null && run.result !== undefined
  const endings = endingsOf(run.result)
  return (
    <>
      <SubmissionLayout
        aside={
          <AsideSection title="Timing">
            <AsideFact label="Started">
              {formatDateTime(run.created_at)}
            </AsideFact>
            <AsideFact label="Picked up">
              {run.started_at ? formatDateTime(run.started_at) : "Not yet"}
            </AsideFact>
            {run.finished_at && (
              <AsideFact label="Ended">
                {formatDateTime(run.finished_at)}
              </AsideFact>
            )}
            <AsideFact label="Duration">
              {run.duration_ms === null ? "—" : formatDuration(run.duration_ms)}
            </AsideFact>
            <AsideFact label="Attempt">{run.attempt}</AsideFact>
          </AsideSection>
        }
      >
        <StatusNotice organizationId={organizationId} run={run} />
        <PageSection
          title="What it was given"
          meta={
            <CopyButton value={json(run.input)} label="Copy input" size="xs" />
          }
          description="The input its start received."
        >
          <JsonView value={run.input ?? null} className="max-h-[28rem]" />
        </PageSection>
        {run.files?.length ? (
          <PageSection
            title="Files it started with"
            description="Saved in the run's artifact store, where its steps read them by name (state.files lists them)."
          >
            <RunFiles
              organizationId={organizationId}
              runId={run.id}
              files={run.files}
              onPreview={setPreviewing}
            />
          </PageSection>
        ) : null}
        {saved.length > 0 && (
          <PageSection
            title="Files it saved"
            description="What its agents saved with save_file, in its session: later steps read them with load_artifacts."
          >
            <SavedFiles
              organizationId={organizationId}
              runId={run.id}
              files={saved}
              doc={doc}
              onPreview={setPreviewing}
            />
          </PageSection>
        )}
        {(hasResult || (ended && run.status === "succeeded")) && (
          <PageSection
            title="What it handed on"
            meta={
              hasResult ? (
                <CopyButton
                  value={json(run.result)}
                  label="Copy result"
                  size="xs"
                />
              ) : undefined
            }
            description={
              endings
                ? `${endings.length} ways ended, in the order they did: each one's step, how it ended and its result.`
                : "Its End's result."
            }
          >
            {endings ? (
              <Endings endings={endings} />
            ) : hasResult ? (
              <JsonView value={run.result} className="max-h-[28rem]" />
            ) : (
              <p className="text-[0.8125rem] text-muted-foreground">
                It ended without handing anything on.
              </p>
            )}
          </PageSection>
        )}
        {run.children?.length ? (
          <PageSection
            title="Runs it started"
            description="Workflows its LLM agents called as tools, each a run of its own. Abandoning this run abandons those still going."
          >
            <RecordList>
              {run.children.map((child) => {
                const shown = runStatusDisplay(child)
                return (
                  <RecordRow
                    key={child.id}
                    label={child.agent_name || child.agent_id}
                  >
                    <span className="flex flex-wrap items-center gap-2">
                      <StatusBadge status={shown.status}>
                        {shown.label}
                      </StatusBadge>
                      <Button
                        variant="link"
                        size="xs"
                        className="h-auto p-0 font-mono"
                        onClick={() => onOpenRun(child.id)}
                      >
                        {shortId(child.id)}
                      </Button>
                      <span className="text-muted-foreground">
                        {formatDateTime(child.created_at)}
                      </span>
                    </span>
                  </RecordRow>
                )
              })}
            </RecordList>
          </PageSection>
        ) : null}
        <PageSection title="Record">
          <RecordList>
            <RecordRow label="Run ID">
              <code>{run.id}</code>
            </RecordRow>
            <RecordRow label="Workflow">
              {run.agent_name || run.agent_id} ·{" "}
              {versionLabel(run).toLowerCase()}
            </RecordRow>
            <RecordRow label="Run by">
              {run.requested_by.name || run.requested_by.id || "Unknown"}
              {run.resubmit_of
                ? " · Resubmitted"
                : triggerLabel(run.trigger)
                  ? ` · ${triggerLabel(run.trigger)}`
                  : ""}
            </RecordRow>
            {run.parent_run_id && (
              <RecordRow label="Started by run">
                <Button
                  variant="link"
                  size="xs"
                  className="h-auto p-0 font-mono"
                  onClick={() => onOpenRun(run.parent_run_id!)}
                >
                  {shortId(run.parent_run_id)}
                </Button>
              </RecordRow>
            )}
            {run.resubmit_of && (
              <RecordRow label="Resubmitted from">
                <Button
                  variant="link"
                  size="xs"
                  className="h-auto p-0 font-mono"
                  onClick={() => onOpenRun(run.resubmit_of!)}
                >
                  {shortId(run.resubmit_of)}
                </Button>
              </RecordRow>
            )}
            <RecordRow label="ADK session">
              <code>{run.session_id}</code>
            </RecordRow>
            <RecordRow label="Last updated">
              {formatDateTime(run.updated_at)}
            </RecordRow>
          </RecordList>
        </PageSection>
      </SubmissionLayout>
      <RunFileViewer
        organizationId={organizationId}
        runId={run.id}
        files={previewable}
        open={previewing}
        onOpen={setPreviewing}
        onClose={() => setPreviewing(undefined)}
      />
    </>
  )
}

/* -------------------------------------------------------------------------- */
/* Steps                                                                      */
/* -------------------------------------------------------------------------- */

/** What a step handed on, or why it failed: text as it is, else JSON. */
function StepValue({
  summary,
  value,
  defaultOpen,
}: {
  summary: string
  value: unknown
  defaultOpen?: boolean
}) {
  return (
    <Disclosure summary={summary} defaultOpen={defaultOpen} className="mt-1">
      <pre>
        {typeof value === "string" ? value : JSON.stringify(value, null, 2)}
      </pre>
    </Disclosure>
  )
}

function Step({
  step,
  index,
  doc,
  onTrace,
  onOpenRun,
}: {
  step: AdkRunStep
  index: number
  doc: AgentDocument | undefined
  /** Open the Trace tab on this step's events; absent for those who can't. */
  onTrace?: (stepId: string) => void
  /** Open a run its agents started (a workflow they called). */
  onOpenRun?: (runId: string) => void
}) {
  const kind = stepKindOf(step, doc)
  const info = kind && kindInfo(kind)
  const shown = stepStatusOf(step.status)
  const hasOutput = step.output !== null && step.output !== undefined
  const hasInput = step.input !== null && step.input !== undefined
  const hasCalledWith =
    step.called_with !== null && step.called_with !== undefined
  const changes = step.state_changes ?? []
  const error = stepErrorOf(step.error)
  // A step that took its Error way is done, and hands on its error.
  const failed = step.status === "failed"
  return (
    <ExecutionStep
      step={String(index + 1).padStart(2, "0")}
      completed={step.status === "done"}
      name={
        <span className="inline-flex min-w-0 items-center gap-2">
          {info ? (
            <KindGlyph info={info} size="sm" />
          ) : (
            <span
              aria-hidden="true"
              className="grid size-6 shrink-0 place-items-center rounded-(--radius-soft) border bg-background text-muted-foreground"
            >
              <Icon icon="layers" size={13} />
            </span>
          )}
          <span className="wrap-anywhere">{stepNameOf(step, doc)}</span>
        </span>
      }
      role={info?.label ?? humanize(step.kind || "step")}
      status={<Chip tone={shown.tone}>{shown.label}</Chip>}
      detail={stepTiming(step)}
    >
      {error && (
        <p
          className={cn(
            "flex items-start gap-1.5 text-sm/[1.6] wrap-anywhere",
            failed ? "text-destructive" : "text-muted-foreground"
          )}
        >
          <Icon
            icon={failed ? "failed" : "warning"}
            size={14}
            className="mt-[0.2rem] shrink-0"
          />
          <span>
            {failed ? error.message : `Took its Error way: ${error.message}`}
            {error.code && (
              <code className="ml-1.5 text-xs text-muted-foreground">
                {error.code}
              </code>
            )}
          </span>
        </p>
      )}
      {error?.more !== undefined && error.more !== null && (
        <StepValue summary="Error details" value={error.more} />
      )}
      {hasInput && (
        <StepValue
          summary={
            step.input_from
              ? `Input, from ${stepNameOf({ id: step.input_from, name: "", kind: "" }, doc)}`
              : "Input"
          }
          value={step.input}
        />
      )}
      {hasCalledWith && (
        <StepValue
          summary={calledWithTitle(kind)}
          value={step.called_with}
          // What it sent is what to check first when it failed.
          defaultOpen={failed}
        />
      )}
      {step.calls && step.calls.length > 0 && (
        <ToolCalls calls={step.calls} onOpenRun={onOpenRun} />
      )}
      {hasOutput && (
        <StepValue
          summary="Output"
          value={step.output}
          // An End's output is the run's result.
          defaultOpen={kind === "end"}
        />
      )}
      {changes.length > 0 && <StateChanges changes={changes} />}
      {onTrace && step.status !== "not_reached" && (
        <Button
          variant="link"
          size="xs"
          className="mt-1.5 h-auto p-0"
          onClick={() => onTrace(step.id)}
        >
          <Icon icon="view" data-icon="inline-start" />
          Every event of this step
        </Button>
      )}
    </ExecutionStep>
  )
}

/** What a step set in the session's state: each key, in order, with its value. */
function StateChanges({ changes }: { changes: AdkStateChange[] }) {
  const keys = new Set(changes.map((change) => change.key))
  return (
    <Disclosure
      summary={`Changed the state: ${[...keys].join(", ")}`}
      className="mt-1"
    >
      <div className="flex flex-col gap-2.5">
        {changes.map((change, index) => (
          <div key={`${change.key}-${index}`} className="flex flex-col gap-1">
            <span className="flex items-center gap-2 text-xs">
              <code className="text-foreground">{change.key}</code>
              {change.at && (
                <span className="text-muted-foreground">
                  {formatDateTime(change.at)}
                </span>
              )}
            </span>
            <ValueTree
              value={change.value}
              className="max-h-60 bg-background"
            />
          </div>
        ))}
      </div>
    </Disclosure>
  )
}

const CALL_STATUS: Record<
  AdkToolCall["status"],
  { label: string; tone: ChipTone }
> = {
  done: { label: "Answered", tone: "success" },
  failed: { label: "Failed", tone: "danger" },
  waiting: { label: "Waits to be allowed", tone: "notice" },
  running: { label: "Calling", tone: "neutral" },
}

/**
 * The tools a step's agents called: each one's name, who called it, what
 * went wrong for a failed one, the run a workflow's call started, and what
 * went back and forth.
 */
function ToolCalls({
  calls,
  onOpenRun,
}: {
  calls: AdkToolCall[]
  onOpenRun?: (runId: string) => void
}) {
  return (
    <div className="mt-1 flex flex-col gap-1.5">
      <span className="text-xs font-medium text-muted-foreground">
        {calls.length === 1 ? "1 tool call" : `${calls.length} tool calls`}
      </span>
      {calls.map((call) => {
        const shown = CALL_STATUS[call.status]
        const started = callRunId(call)
        return (
          <div
            key={call.id}
            className="flex flex-col gap-1 rounded-(--radius-control) border px-2.5 py-1.5"
          >
            <div className="flex min-w-0 items-center gap-2">
              <code className="min-w-0 truncate font-mono text-xs text-foreground">
                {call.name}
              </code>
              {call.agent && (
                <span className="min-w-0 truncate text-2xs text-muted-foreground">
                  by {call.agent}
                </span>
              )}
              <Chip tone={shown.tone} className="ml-auto shrink-0">
                {shown.label}
              </Chip>
            </div>
            {call.error && (
              <p className="flex items-start gap-1.5 text-xs/[1.55] wrap-anywhere text-destructive">
                <Icon
                  icon="failed"
                  size={13}
                  className="mt-[0.15rem] shrink-0"
                />
                <span>{call.error}</span>
              </p>
            )}
            {started && onOpenRun && (
              <span className="text-xs text-muted-foreground">
                Started run{" "}
                <Button
                  variant="link"
                  size="xs"
                  className="h-auto p-0 font-mono"
                  onClick={() => onOpenRun(started)}
                >
                  {shortId(started)}
                </Button>
              </span>
            )}
            <Disclosure summary="What it was sent, and answered">
              <pre>
                {JSON.stringify(
                  { sent: call.args, answered: call.response },
                  null,
                  2
                )}
              </pre>
            </Disclosure>
          </div>
        )
      })}
    </div>
  )
}

/**
 * The Steps tab: every node of the workflow, in order, as the run went
 * through it (done, failed, waiting or not reached), with what each handed
 * on or why it failed, and when. What the run waits for is answered above
 * them, as on Overview. The nodes' kinds come from the workflow the run
 * started with (else as it's saved now, else as the run says).
 */
function AdkRunSteps({
  organizationId,
  run,
  doc,
  onTrace,
  onOpenRun,
}: {
  organizationId: string
  run: AdkRunDetail
  doc: AgentDocument | undefined
  onTrace?: (stepId: string) => void
  onOpenRun?: (runId: string) => void
}) {
  const steps = useAdkRunSteps(organizationId, run)
  const list = steps.data?.steps

  let body: React.ReactNode
  if (steps.error && !steps.data) {
    body = (
      <ErrorCallout
        title={failureTitle(steps.error, "Couldn't load the run's steps")}
        action={
          <Button
            variant="outline"
            size="sm"
            onClick={() => void steps.refetch()}
          >
            Retry
          </Button>
        }
      >
        {steps.error.message}
      </ErrorCallout>
    )
  } else if (!list) {
    body = (
      <div className="flex flex-col gap-3" aria-busy="true">
        <Skeleton className="h-12 w-full" />
        <Skeleton className="h-12 w-full" />
        <Skeleton className="h-12 w-full" />
      </div>
    )
  } else if (list.length === 0) {
    body = (
      <PanelEmpty illustration="waiting">
        No step has run yet. The steps show here once a worker starts the run.
      </PanelEmpty>
    )
  } else {
    body = (
      <>
        <p className="mb-2 max-w-[75ch] text-[0.8125rem] text-muted-foreground">
          Every node of the workflow, in order, and how far the run got with
          each: what it was handed, what it sent (a saved workflow&apos;s
          inputs, an HTTP request), what it handed on (in a loop, its last
          item&apos;s) or why it failed, and what it changed in the run&apos;s
          state. What runs inside a node, a team&apos;s sub-agents or a saved
          workflow&apos;s steps, counts as that node.
        </p>
        <ExecutionPlan aria-label="Steps" className="max-w-[75ch]">
          {list.map((step, index) => (
            <Step
              key={`${step.id}-${index}`}
              step={step}
              index={index}
              doc={doc}
              onTrace={onTrace}
              onOpenRun={onOpenRun}
            />
          ))}
        </ExecutionPlan>
      </>
    )
  }

  return (
    <div className={BODY}>
      {openGatesOf(run).length > 0 && (
        <AdkGates organizationId={organizationId} run={run} />
      )}
      {body}
    </div>
  )
}

/* -------------------------------------------------------------------------- */
/* Activity                                                                   */
/* -------------------------------------------------------------------------- */

/**
 * The Activity tab: everything recorded about the run, newest first: who
 * started it, when a worker took it, each step's progress, what it waited
 * for and who decided, retries, and how it ended.
 */
function AdkRunActivity({ run }: { run: AdkRunDetail }) {
  const events = React.useMemo(() => [...run.events].reverse(), [run.events])
  return (
    <div className={BODY}>
      {events.length === 0 ? (
        <PanelEmpty illustration="activity">
          Nothing is recorded yet.
        </PanelEmpty>
      ) : (
        <>
          <p className="mb-6 text-[0.8125rem] text-muted-foreground">
            Everything recorded about this run, newest first.
          </p>
          {run.activity_incomplete && (
            <p
              role="note"
              className="mb-6 flex max-w-[75ch] items-center gap-3 rounded-(--radius-card) border bg-notice-surface px-3 py-2 text-xs text-notice-foreground"
            >
              <Icon
                icon="info"
                size={14}
                className="shrink-0 text-notice-accent"
              />
              Some of its progress isn&apos;t listed: it noted as many steps as
              a run may, or some notes couldn&apos;t be saved. Its status,
              result and steps are complete.
            </p>
          )}
          <EventList aria-label="Activity" className="max-w-[75ch]">
            {events.map((event) => (
              <EventItem
                key={event.id}
                type={eventTitle(event)}
                time={formatDateTime(event.at)}
                error={isFailureEvent(event)}
              >
                {[event.message, event.actor?.name && `by ${event.actor.name}`]
                  .filter(Boolean)
                  .join(" · ") || undefined}
                {event.attributes &&
                  Object.keys(event.attributes).length > 0 && (
                    <span className="mt-1 block font-mono text-3xs wrap-anywhere text-subtle">
                      {Object.entries(event.attributes)
                        .map(([key, value]) => `${key}: ${preview(value, 120)}`)
                        .join(" · ")}
                    </span>
                  )}
              </EventItem>
            ))}
          </EventList>
        </>
      )}
    </div>
  )
}

/* -------------------------------------------------------------------------- */
/* Header                                                                     */
/* -------------------------------------------------------------------------- */

/** Confirms giving up on the run (a running one stops first); shows why if it can't. */
function AbandonDialog({
  open,
  onOpenChange,
  organizationId,
  runId,
  running,
  openChildren,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  organizationId: string
  runId: string
  running: boolean
  /** How many runs it started are still going: they're abandoned with it. */
  openChildren: number
}) {
  // The dialog shows the failure itself.
  const abandon = useAbandonAdkRun(organizationId, runId, {
    meta: { silent: true },
  })
  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (abandon.isPending) return
        if (!next) abandon.reset()
        onOpenChange(next)
      }}
    >
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Abandon this run?</DialogTitle>
          <DialogDescription>
            {running
              ? "Its worker stops it at its next step, or within a minute, cutting short the step it's on. Then it's abandoned: it never carries on, and nobody can retry it. You can still resubmit it, which runs it again as a new run."
              : "It stops for good: it never carries on, and nobody can retry it. You can still resubmit it, which runs it again as a new run."}
            {openChildren > 0 &&
              (openChildren === 1
                ? " The run it started that's still going is abandoned too."
                : ` The ${openChildren} runs it started that are still going are abandoned too.`)}
          </DialogDescription>
        </DialogHeader>
        {abandon.error && (
          <ErrorCallout title="Couldn't abandon the run">
            {abandon.error.message}
          </ErrorCallout>
        )}
        <DialogFooter>
          <DialogClose
            render={<Button variant="outline" disabled={abandon.isPending} />}
          >
            Keep it
          </DialogClose>
          <Button
            variant="destructive"
            disabled={abandon.isPending}
            onClick={() =>
              abandon.mutate(undefined, {
                onSuccess: () => onOpenChange(false),
              })
            }
          >
            {abandon.isPending && <Spinner data-icon="inline-start" />}
            Abandon run
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

/**
 * Copy link, and for those who manage the organization's runs what the
 * run's status allows: Retry (its next attempt, the primary action),
 * Resubmit (a new run of the same input) and Abandon.
 */
function RunActions({
  organizationId,
  run,
  canManage,
  onOpenRun,
}: {
  organizationId: string
  run: AdkRunDetail
  canManage: boolean
  onOpenRun: (runId: string) => void
}) {
  const [abandoning, setAbandoning] = React.useState(false)
  const retry = useRetryAdkRun(organizationId, run.id)
  const resubmit = useResubmitAdkRun(organizationId, run.id)
  const busy = retry.isPending || resubmit.isPending
  const { actions } = run
  return (
    <>
      <Button variant="outline" size="sm" onClick={copyLink}>
        <Icon icon="link" data-icon="inline-start" />
        Copy link
      </Button>
      {canManage && actions.resubmit && (
        <Tooltip>
          <TooltipTrigger
            render={
              <Button
                variant="outline"
                size="sm"
                disabled={busy}
                onClick={() =>
                  resubmit.mutate(undefined, {
                    onSuccess: (next) => onOpenRun(next.id),
                  })
                }
              />
            }
          >
            {resubmit.isPending ? (
              <Spinner data-icon="inline-start" />
            ) : (
              <Icon icon="plus" data-icon="inline-start" />
            )}
            Resubmit
          </TooltipTrigger>
          <TooltipContent className="max-w-72">
            Runs the same workflow with the same input again, as a new run.
          </TooltipContent>
        </Tooltip>
      )}
      {canManage && actions.retry && (
        <Tooltip>
          <TooltipTrigger
            render={
              <Button
                size="sm"
                disabled={busy}
                onClick={() => retry.mutate()}
              />
            }
          >
            {retry.isPending ? (
              <Spinner data-icon="inline-start" />
            ) : (
              <Icon icon="refresh" data-icon="inline-start" />
            )}
            Retry
          </TooltipTrigger>
          <TooltipContent className="max-w-72">
            Carries it on as attempt {run.attempt + 1}, from where it stopped.
          </TooltipContent>
        </Tooltip>
      )}
      {canManage && actions.abandon && (
        <Button
          variant="destructive"
          size="sm"
          disabled={busy}
          onClick={() => setAbandoning(true)}
        >
          <Icon icon="stop" data-icon="inline-start" />
          Abandon
        </Button>
      )}
      {/* Mounted while open: abandoning takes Abandon away, and the dialog
          still has to close itself. */}
      {canManage && (actions.abandon || abandoning) && (
        <AbandonDialog
          open={abandoning}
          onOpenChange={setAbandoning}
          organizationId={organizationId}
          runId={run.id}
          running={run.status === "running"}
          openChildren={run.children?.filter(isOpenRun).length ?? 0}
        />
      )}
    </>
  )
}

function HeaderSkeleton() {
  return (
    <TaskPageHeader>
      <Skeleton className="mb-[19px] h-7 w-48" />
      <Skeleton className="mb-3.5 h-8 w-[min(36rem,100%)]" />
      <div className="flex gap-3">
        <Skeleton className="h-6 w-24" />
        <Skeleton className="h-6 w-40" />
        <Skeleton className="h-6 w-20" />
      </div>
    </TaskPageHeader>
  )
}

/**
 * One of the organization's workflow runs, under workflows: its
 * header (status, attempt, when it last moved) with Copy link and, for
 * those who manage the organization's runs (`agents:manage_runs`), Retry,
 * Resubmit and Abandon as its status allows; then Overview (what it waits
 * for, its input, result and record), Steps (each node, as the run went
 * through it: its input, output and state changes), Activity, and for
 * those who manage runs Trace (every event of its ADK session, and its
 * state at each). What it waits for is decided or answered in
 * Overview and Steps. It follows a run in progress every few seconds.
 */
export function AdkRunPage({
  organization,
  runId,
  tab,
  canManage,
  onTabChange,
  onBack,
  onOpenRun,
}: {
  organization: MyOrganization
  runId: string
  tab: AdkRunTab
  /** `agents:manage_runs` in the organization: offer the actions. */
  canManage: boolean
  onTabChange: (tab: AdkRunTab) => void
  onBack: () => void
  /** Open another run's page (a resubmitted one, the one it resubmitted). */
  onOpenRun: (runId: string) => void
}) {
  const detail = useAdkRun(organization.id, runId)
  const run = detail.data
  const doc = useRunDocument(organization.id, run)
  // Its steps are shown by their kinds: it opens once they're loaded too.
  const catalogs = useNodeCatalogs()
  // The step the Trace tab's timeline is narrowed to.
  const [traceStep, setTraceStep] = React.useState<string>()
  const reference = shortId(runId)
  const section = WORKSPACE_VIEWS.agents
  const shown = run && runStatusDisplay(run)

  useShellPage({
    header: {
      title: reference,
      icon: section.icon,
      breadcrumbs: [
        { label: organization.name, href: `/organizations/${organization.id}` },
        { label: section.label, icon: section.icon, onSelect: onBack },
      ],
    },
  })
  // For the assistant: the run and the tab.
  usePageContext({
    entities: [
      {
        kind: "adk_run",
        id: runId,
        label: run
          ? `${reference}: ${run.agent_name || run.agent_id}`
          : reference,
        detail: shown?.label,
      },
    ],
    view: { tab },
  })

  if (!run || !shown || !catalogs.ready) {
    if (detail.error?.status === 404) {
      return (
        <PageEmpty
          illustration="search"
          title="Workflow run not found"
          description="It isn't one of this organization's runs, or it no longer exists."
        >
          <Button variant="outline" size="sm" onClick={onBack}>
            <Icon icon="left" data-icon="inline-start" />
            All workflow runs
          </Button>
        </PageEmpty>
      )
    }
    if (detail.error) {
      return (
        <ErrorCallout
          title={failureTitle(detail.error, "Couldn't load the workflow run")}
          action={
            <Button
              variant="outline"
              size="sm"
              onClick={() => void detail.refetch()}
            >
              Retry
            </Button>
          }
        >
          {detail.error.message}
        </ErrorCallout>
      )
    }
    if (catalogs.error) {
      return (
        <ErrorCallout
          title="Couldn't load the kinds of node"
          action={
            <Button variant="outline" size="sm" onClick={catalogs.retry}>
              Retry
            </Button>
          }
        >
          {catalogs.error.message}
        </ErrorCallout>
      )
    }
    return (
      <FullBleed>
        <TaskPage aria-busy="true">
          <HeaderSkeleton />
        </TaskPage>
      </FullBleed>
    )
  }

  const live = isLiveRun(run)

  return (
    <FullBleed>
      <TaskPage>
        <TaskPageHeader>
          <TaskPageEyebrow
            onBack={onBack}
            backLabel="All workflow runs"
            reference={reference}
          />
          <TaskTitleRow
            title={run.agent_name || run.agent_id}
            meta={
              <>
                <StatusBadge status={shown.status} size="lg">
                  {shown.label}
                </StatusBadge>
                <TaskMeta icon={section.icon}>{versionLabel(run)}</TaskMeta>
                <TaskMeta icon="refresh">
                  {run.attempt === 1
                    ? "First attempt"
                    : `Attempt ${run.attempt}`}
                </TaskMeta>
                <TaskMeta icon="clock">
                  Updated {formatRelative(run.updated_at)}
                </TaskMeta>
              </>
            }
            actions={
              <RunActions
                organizationId={organization.id}
                run={run}
                canManage={canManage}
                onOpenRun={onOpenRun}
              />
            }
          />
        </TaskPageHeader>
        <Tabs
          value={tab}
          onValueChange={(value) => {
            if (isAdkRunTab(value)) onTabChange(value)
          }}
          className="gap-0"
        >
          <TaskTabBar>
            <TaskTabsList aria-label="Run pages">
              <TaskTabsTrigger value="overview" icon="task">
                Overview
              </TaskTabsTrigger>
              <TaskTabsTrigger
                value="steps"
                icon={LeftToRightListNumberIcon}
                live={live}
              >
                Steps
              </TaskTabsTrigger>
              <TaskTabsTrigger value="activity" icon="activity">
                Activity
              </TaskTabsTrigger>
              {canManage && (
                <TaskTabsTrigger value="trace" icon="code" live={live}>
                  Trace
                </TaskTabsTrigger>
              )}
            </TaskTabsList>
            {live && <LiveLabel>Live</LiveLabel>}
          </TaskTabBar>
          <TabsContent value="overview">
            <AdkRunOverview
              organizationId={organization.id}
              run={run}
              doc={doc}
              onOpenRun={onOpenRun}
            />
          </TabsContent>
          <TabsContent value="steps">
            <AdkRunSteps
              organizationId={organization.id}
              run={run}
              doc={doc}
              onOpenRun={onOpenRun}
              onTrace={
                canManage
                  ? (stepId) => {
                      setTraceStep(stepId)
                      onTabChange("trace")
                    }
                  : undefined
              }
            />
          </TabsContent>
          <TabsContent value="activity">
            <AdkRunActivity run={run} />
          </TabsContent>
          <TabsContent value="trace">
            <div className={TRACE_BODY}>
              {canManage ? (
                <AdkRunTraceTab
                  organizationId={organization.id}
                  run={run}
                  doc={doc}
                  step={traceStep}
                  onStepChange={setTraceStep}
                />
              ) : (
                <PanelEmpty illustration="locked">
                  Only those who manage the organization&apos;s runs see their
                  trace.
                </PanelEmpty>
              )}
            </div>
          </TabsContent>
        </Tabs>
      </TaskPage>
    </FullBleed>
  )
}
