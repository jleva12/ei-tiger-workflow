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
import { ApprovalPanel, HumanInputPanel } from "@/features/runs/components/pause-panels"
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
import { organizationAgents } from "@/features/adk-workflows/lib/api"
import type { AgentDocument } from "@/features/adk-workflows/lib/document"
import { AGENT_KINDS } from "@/features/adk-workflows/lib/model"
import {
  adkApprovePermission,
  eventTitle,
  failureCategory,
  failureTitle,
  humanize,
  isAdkRunTab,
  isFailureEvent,
  pauseKindOf,
  responseSchemaOf,
  runDocumentOf,
  shortId,
  stepErrorOf,
  stepKindOf,
  stepNameOf,
  stepStatusOf,
  stepTiming,
  useAbandonAdkRun,
  useAdkRun,
  useAdkRunSteps,
  useAnswerAdkRun,
  useDecideAdkRun,
  useResubmitAdkRun,
  useRetryAdkRun,
  type AdkRunDetail,
  type AdkRunPause,
  type AdkRunStep,
  type AdkRunTab,
} from "@/features/runs/lib/runs"
import { formatDateTime, formatDuration, formatRelative } from "@/lib/format"
import { WORKSPACE_VIEWS } from "@/features/organizations/lib/organization-workspace"
import { usePageContext } from "@/features/assistant/lib/page-context"
import { isLiveRun, runStatusDisplay } from "@/features/runs/lib/display"

// A tab's body, on the same gutters as the header and tabs above it.
const BODY =
  "w-full max-w-[1100px] px-[30px] pt-7 pb-[60px] @max-[900px]/shell:px-5 @max-[900px]/shell:py-6 @max-[600px]/shell:px-[15px] @max-[600px]/shell:py-5"

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

/**
 * Where the run stands, when that needs saying: what it waits for (a
 * person, a time, a worker), the hiccup it's retried after, or why it failed.
 */
function StatusNotice({
  organizationId,
  run,
}: {
  organizationId: string
  run: AdkRunDetail
}) {
  if (run.status === "paused" && run.pause)
    return (
      <AdkPausePanel organizationId={organizationId} runId={run.id} pause={run.pause} />
    )
  if (run.status === "waiting" && run.waiting_until) {
    return (
      <WaitingNotice title={`Carries on ${formatRelative(run.waiting_until)}`} state="Waiting">
        {run.waiting_reason ? `${sentence(run.waiting_reason)}. ` : ""}It carries on by
        itself at {formatDateTime(run.waiting_until)}. No worker is busy with it
        meanwhile.
      </WaitingNotice>
    )
  }
  const error = run.error
  if (error && isLiveRun(run)) {
    const category = failureCategory(error.category)
    return (
      <WaitingNotice icon="refresh" title="Running again after a hiccup" state={category.label}>
        The last one: {sentence(error.message)}. {category.description}
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
  if (error && (run.status === "failed" || run.status === "abandoned")) {
    const category = failureCategory(error.category)
    return (
      <ErrorCallout
        title={run.status === "failed" ? "The run failed" : "The run was abandoned after failing"}
        className="mb-[34px]"
      >
        <span className="flex flex-col gap-1">
          <span>{error.message}</span>
          <span>
            <strong className="font-medium">{category.label}.</strong> {category.description}
          </span>
        </span>
      </ErrorCallout>
    )
  }
  if (run.status === "failed") {
    return (
      <ErrorCallout title="The run failed" className="mb-[34px]">
        No reason was recorded.
      </ErrorCallout>
    )
  }
  return null
}

/* -------------------------------------------------------------------------- */
/* Overview                                                                   */
/* -------------------------------------------------------------------------- */

const json = (value: unknown) => JSON.stringify(value ?? null, null, 2)

/**
 * The Overview tab: what the run waits for or why it failed, what it was
 * given and what it handed on, and its record, with its timing beside them.
 */
function AdkRunOverview({
  organizationId,
  run,
  onOpenRun,
}: {
  organizationId: string
  run: AdkRunDetail
  onOpenRun: (runId: string) => void
}) {
  const ended = run.finished_at !== null
  const hasResult = run.result !== null && run.result !== undefined
  return (
    <SubmissionLayout
      aside={
        <AsideSection title="Timing">
          <AsideFact label="Started">{formatDateTime(run.created_at)}</AsideFact>
          <AsideFact label="Picked up">
            {run.started_at ? formatDateTime(run.started_at) : "Not yet"}
          </AsideFact>
          {run.finished_at && (
            <AsideFact label="Ended">{formatDateTime(run.finished_at)}</AsideFact>
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
        meta={<CopyButton value={json(run.input)} label="Copy input" size="xs" />}
        description="The input its start received."
      >
        <JsonView value={run.input ?? null} className="max-h-[28rem]" />
      </PageSection>
      {(hasResult || (ended && run.status === "succeeded")) && (
        <PageSection
          title="What it handed on"
          meta={
            hasResult ? (
              <CopyButton value={json(run.result)} label="Copy result" size="xs" />
            ) : undefined
          }
          description="Its End's result."
        >
          {hasResult ? (
            <JsonView value={run.result} className="max-h-[28rem]" />
          ) : (
            <p className="text-[0.8125rem] text-muted-foreground">
              It ended without handing anything on.
            </p>
          )}
        </PageSection>
      )}
      <PageSection title="Record">
        <RecordList>
          <RecordRow label="Run ID">
            <code>{run.id}</code>
          </RecordRow>
          <RecordRow label="ADK workflow">
            {run.agent_name || run.agent_id} · revision {run.revision}
          </RecordRow>
          <RecordRow label="Run by">
            {run.requested_by.name || run.requested_by.id || "Unknown"}
          </RecordRow>
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
          <RecordRow label="Last updated">{formatDateTime(run.updated_at)}</RecordRow>
        </RecordList>
      </PageSection>
    </SubmissionLayout>
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
      <pre>{typeof value === "string" ? value : JSON.stringify(value, null, 2)}</pre>
    </Disclosure>
  )
}

function Step({
  step,
  index,
  doc,
}: {
  step: AdkRunStep
  index: number
  doc: AgentDocument | undefined
}) {
  const kind = stepKindOf(step, doc)
  const info = kind && AGENT_KINDS[kind]
  const shown = stepStatusOf(step.status)
  const hasOutput = step.output !== null && step.output !== undefined
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
          <Icon icon={failed ? "failed" : "warning"} size={14} className="mt-[0.2rem] shrink-0" />
          <span>
            {failed ? error.message : `Took its Error way: ${error.message}`}
            {error.code && (
              <code className="ml-1.5 text-xs text-muted-foreground">{error.code}</code>
            )}
          </span>
        </p>
      )}
      {error?.more !== undefined && error.more !== null && (
        <StepValue summary="Error details" value={error.more} />
      )}
      {hasOutput && (
        <StepValue
          summary="Output"
          value={step.output}
          // An End's output is the run's result.
          defaultOpen={kind === "end"}
        />
      )}
    </ExecutionStep>
  )
}

/**
 * The Steps tab: every node of the ADK workflow, in order, as the run went
 * through it (done, failed, waiting or not reached), with what each handed
 * on or why it failed, and when. What the run waits for is answered above
 * them, as on Overview. The nodes' kinds come from the ADK workflow the run
 * started with (else as it's saved now, else as the run says).
 */
function AdkRunSteps({ organizationId, run }: { organizationId: string; run: AdkRunDetail }) {
  const steps = useAdkRunSteps(organizationId, run)
  const snapshot = runDocumentOf(run.document)
  const saved = organizationAgents
    .scope({ organizationId })
    .useDetail(snapshot ? undefined : run.agent_id || undefined)
  const doc = snapshot ?? saved.data?.document
  const list = steps.data?.steps

  let body: React.ReactNode
  if (steps.error && !steps.data) {
    body = (
      <ErrorCallout
        title={failureTitle(steps.error, "Couldn't load the run's steps")}
        action={
          <Button variant="outline" size="sm" onClick={() => void steps.refetch()}>
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
          Every node of the ADK workflow, in order, and how far the run got with each:
          what it handed on (in a loop, its last item&apos;s), or why it failed. What
          runs inside a node, a team&apos;s sub-agents or a saved workflow&apos;s steps,
          counts as that node.
        </p>
        <ExecutionPlan aria-label="Steps" className="max-w-[75ch]">
          {list.map((step, index) => (
            <Step key={`${step.id}-${index}`} step={step} index={index} doc={doc} />
          ))}
        </ExecutionPlan>
      </>
    )
  }

  return (
    <div className={BODY}>
      {run.status === "paused" && run.pause && (
        <AdkPausePanel organizationId={organizationId} runId={run.id} pause={run.pause} />
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
        <PanelEmpty illustration="activity">Nothing is recorded yet.</PanelEmpty>
      ) : (
        <>
          <p className="mb-6 text-[0.8125rem] text-muted-foreground">
            Everything recorded about this run, newest first.
          </p>
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

/** Confirms giving up on the run; shows why if it can't. */
function AbandonDialog({
  open,
  onOpenChange,
  organizationId,
  runId,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  organizationId: string
  runId: string
}) {
  // The dialog shows the failure itself.
  const abandon = useAbandonAdkRun(organizationId, runId, { meta: { silent: true } })
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
            It stops for good: it never carries on, and nobody can retry it. You can
            still resubmit it, which runs it again as a new run.
          </DialogDescription>
        </DialogHeader>
        {abandon.error && (
          <ErrorCallout title="Couldn't abandon the run">{abandon.error.message}</ErrorCallout>
        )}
        <DialogFooter>
          <DialogClose render={<Button variant="outline" disabled={abandon.isPending} />}>
            Keep it
          </DialogClose>
          <Button
            variant="destructive"
            disabled={abandon.isPending}
            onClick={() => abandon.mutate(undefined, { onSuccess: () => onOpenChange(false) })}
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
                onClick={() => resubmit.mutate(undefined, { onSuccess: (next) => onOpenRun(next.id) })}
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
            Runs the same ADK workflow with the same input again, as a new run.
          </TooltipContent>
        </Tooltip>
      )}
      {canManage && actions.retry && (
        <Tooltip>
          <TooltipTrigger
            render={<Button size="sm" disabled={busy} onClick={() => retry.mutate()} />}
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
        <Button variant="destructive" size="sm" disabled={busy} onClick={() => setAbandoning(true)}>
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
 * One of the organization's ADK workflow runs, under ADK workflows: its
 * header (status, attempt, when it last moved) with Copy link and, for
 * those who manage the organization's runs (`agents:manage_runs`), Retry,
 * Resubmit and Abandon as its status allows; then Overview (what it waits
 * for, its input, result and record), Steps (each node, as the run went
 * through it) and Activity. What it waits for is decided or answered in
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
        label: run ? `${reference}: ${run.agent_name || run.agent_id}` : reference,
        detail: shown?.label,
      },
    ],
    view: { tab },
  })

  if (!run || !shown) {
    if (detail.error?.status === 404) {
      return (
        <PageEmpty
          illustration="search"
          title="ADK workflow run not found"
          description="It isn't one of this organization's runs, or it no longer exists."
        >
          <Button variant="outline" size="sm" onClick={onBack}>
            <Icon icon="left" data-icon="inline-start" />
            All ADK workflow runs
          </Button>
        </PageEmpty>
      )
    }
    if (detail.error) {
      return (
        <ErrorCallout
          title={failureTitle(detail.error, "Couldn't load the ADK workflow run")}
          action={
            <Button variant="outline" size="sm" onClick={() => void detail.refetch()}>
              Retry
            </Button>
          }
        >
          {detail.error.message}
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
          <TaskPageEyebrow onBack={onBack} backLabel="All ADK workflow runs" reference={reference} />
          <TaskTitleRow
            title={run.agent_name || run.agent_id}
            meta={
              <>
                <StatusBadge status={shown.status} size="lg">
                  {shown.label}
                </StatusBadge>
                <TaskMeta icon={section.icon}>Revision {run.revision}</TaskMeta>
                <TaskMeta icon="refresh">
                  {run.attempt === 1 ? "First attempt" : `Attempt ${run.attempt}`}
                </TaskMeta>
                <TaskMeta icon="clock">Updated {formatRelative(run.updated_at)}</TaskMeta>
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
              <TaskTabsTrigger value="steps" icon={LeftToRightListNumberIcon} live={live}>
                Steps
              </TaskTabsTrigger>
              <TaskTabsTrigger value="activity" icon="activity">
                Activity
              </TaskTabsTrigger>
            </TaskTabsList>
            {live && <LiveLabel>Live</LiveLabel>}
          </TaskTabBar>
          <TabsContent value="overview">
            <AdkRunOverview organizationId={organization.id} run={run} onOpenRun={onOpenRun} />
          </TabsContent>
          <TabsContent value="steps">
            <AdkRunSteps organizationId={organization.id} run={run} />
          </TabsContent>
          <TabsContent value="activity">
            <AdkRunActivity run={run} />
          </TabsContent>
        </Tabs>
      </TaskPage>
    </FullBleed>
  )
}
