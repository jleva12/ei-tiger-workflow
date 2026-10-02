import * as React from "react"

import { CopyButton, JsonView } from "@/components/json/json-view"
import { EventItem, EventList } from "@/components/forge/activity"
import { PanelEmpty } from "@/components/forge/empty-state"
import { ErrorCallout } from "@/components/forge/feedback"
import { Icon } from "@/components/forge/icon"
import { Chip, StatusBadge } from "@/components/forge/status"
import {
  AsideFact,
  AsideSection,
  Disclosure,
  ExecutionPlan,
  ExecutionStep,
  PageSection,
  RecordList,
  RecordRow,
  SubmissionLayout,
  WaitingNotice,
} from "@/components/forge/task-page"
import {
  actorLabel,
  eventTitle,
  failureCategory,
  humanize,
  isFailureEvent,
  isLiveStatus,
  kindLabel,
  OUTCOME_NAMES,
  restartEventOf,
  statusDisplay,
  taskTypeOf,
  type BackgroundTaskApproval,
  type BackgroundTaskDetail,
  type BackgroundTaskEvent,
  type BackgroundTaskResult,
  type BackgroundTaskRun,
  type BackgroundTaskRunFailure,
  type BackgroundTaskStatus,
  type BackgroundTaskStep,
} from "@/lib/background-tasks"
import { formatDateTime, formatDuration, formatRelative } from "@/lib/format"
import { userName, users } from "@/lib/users"

// A tab's body, on the same gutters as the header and tabs above it.
const BODY =
  "w-full max-w-[1100px] px-[30px] pt-7 pb-[60px] @max-[900px]/shell:px-5 @max-[900px]/shell:py-6 @max-[600px]/shell:px-[15px] @max-[600px]/shell:py-5"

/** A tab's body, for the tabs a kind of task adds (`TaskPane`). */
export function TaskPaneBody({ children }: { children: React.ReactNode }) {
  return <div className={BODY}>{children}</div>
}

type PersonName = (id: string) => string | undefined

/** Who a person is, by their Forge user ID: their name once users load. */
function usePersonName(): PersonName {
  const { data } = users.useList()
  return React.useCallback(
    (id: string) => {
      const user = data?.find((found) => found.id === id)
      return user ? userName(user) : undefined
    },
    [data]
  )
}

const duration = (ms: number | null) => (ms === null ? "—" : formatDuration(ms))

// "Primary stepped down" reads on into the next sentence without "..".
const sentence = (text: string) => text.trim().replace(/[.\s]+$/, "")

const statusName = (status: BackgroundTaskStatus) => statusDisplay(status).label

/**
 * What answers the approval a task waits for, given the task: its page's
 * own panel (an ADK workflow run's approval or question).
 */
export type ApprovalPanelOf = (
  task: BackgroundTaskDetail & { approval: BackgroundTaskApproval }
) => React.ReactNode

/* -------------------------------------------------------------------------- */
/* Overview                                                                   */
/* -------------------------------------------------------------------------- */

const FAILED_TITLES: Partial<Record<BackgroundTaskStatus, string>> = {
  FAILED: "The task failed",
  STOPPED: "The task stopped",
  ABANDONED: "The task was abandoned",
}

/**
 * Where the task stands, when that needs saying: waiting for a worker,
 * retrying after a failure, or failed and why.
 */
function StatusNotice({
  task,
  approvalPanel,
}: {
  task: BackgroundTaskDetail
  approvalPanel?: ApprovalPanelOf
}) {
  if (task.approval && task.status === "AWAITING_VALIDATION" && approvalPanel)
    return approvalPanel({ ...task, approval: task.approval })
  if (task.status === "STOPPED" && task.waiting_until) {
    return (
      <WaitingNotice
        title={`Carries on ${formatRelative(task.waiting_until)}`}
        state="Waiting"
      >
        It&apos;s waiting for {task.waiting_reason || "a time"}, and carries on
        by itself at {formatDateTime(task.waiting_until)}. No worker is busy
        with it meanwhile.
      </WaitingNotice>
    )
  }
  const failure = task.failure
  if (failure && isLiveStatus(task.status)) {
    const category = failureCategory(failure.category)
    return (
      <WaitingNotice
        icon="refresh"
        title="Running again after a failure"
        state={category.label}
      >
        The last failure: {sentence(failure.message)}. {category.description}
      </WaitingNotice>
    )
  }
  if (task.status === "PENDING") {
    return (
      <WaitingNotice title="Waiting for a worker" state="Queued">
        It's queued{task.delivery ? ` on ${task.delivery.queue}` : ""}. A worker
        picks it up shortly.
      </WaitingNotice>
    )
  }
  if (failure && task.status !== "COMPLETED") {
    const category = failureCategory(failure.category)
    return (
      <ErrorCallout
        title={FAILED_TITLES[task.status] ?? "The last attempt failed"}
        className="mb-[34px]"
      >
        <span className="flex flex-col gap-1">
          <span>{failure.message}</span>
          <span>
            <strong className="font-medium">{category.label}.</strong>{" "}
            {category.description}
          </span>
        </span>
      </ErrorCallout>
    )
  }
  if (task.status === "FAILED") {
    return (
      <ErrorCallout title="The task failed" className="mb-[34px]">
        No reason was recorded.
      </ErrorCallout>
    )
  }
  return null
}

/** A value the job reported, readably: durations, counts, IDs, JSON. */
function DetailValue({ name, value }: { name: string; value: unknown }) {
  if (value === null || value === undefined || value === "") return "—"
  if (typeof value === "number") {
    return name.endsWith("_ms")
      ? formatDuration(value)
      : value.toLocaleString(undefined, { maximumFractionDigits: 2 })
  }
  if (typeof value === "boolean") return value ? "Yes" : "No"
  if (typeof value === "string")
    return name === "id" || name.endsWith("_id") ? <code>{value}</code> : value
  return <code>{JSON.stringify(value)}</code>
}

function ResultSection({ result }: { result: BackgroundTaskResult }) {
  const details = Object.entries(result.detail ?? {})
  const followups = result.followups ?? []
  return (
    <PageSection
      title="Result"
      meta={OUTCOME_NAMES[result.status] ?? humanize(String(result.status))}
    >
      {result.error !== null && result.error !== undefined && (
        <ErrorCallout title="The job reported an error" className="mb-4">
          {typeof result.error === "string"
            ? result.error
            : JSON.stringify(result.error)}
        </ErrorCallout>
      )}
      {details.length > 0 ? (
        <RecordList>
          {details.map(([name, value]) => (
            <RecordRow key={name} label={humanize(name)}>
              <DetailValue name={name} value={value} />
            </RecordRow>
          ))}
        </RecordList>
      ) : (
        <p className="text-[0.8125rem] text-muted-foreground">
          The job reported no details.
        </p>
      )}
      {followups.length > 0 && (
        <div className="mt-6">
          <h3 className="mb-1 text-sm font-medium">
            Follow-ups ({followups.length})
          </h3>
          <p className="mb-3 text-[0.8125rem] text-muted-foreground">
            Tasks it asked for when it finished.
          </p>
          <ul className="flex flex-col divide-y rounded-(--radius-card) border">
            {followups.map((followup, index) => {
              const type = taskTypeOf(followup.task_type)
              return (
                <li key={index} className="flex flex-col gap-2 px-4 py-3">
                  <span className="flex items-center gap-2 text-sm">
                    <Icon icon={type.icon} size={14} />
                    {type.label} · {kindLabel(followup.kind)}
                  </span>
                  {Object.keys(followup.payload ?? {}).length > 0 && (
                    <Disclosure summary="Its input">
                      <pre>{JSON.stringify(followup.payload, null, 2)}</pre>
                    </Disclosure>
                  )}
                </li>
              )
            })}
          </ul>
        </div>
      )}
    </PageSection>
  )
}

/**
 * The Overview tab: what the task was asked to do (its input), how it
 * ended (the job's result and the tasks it asked for), and its record, with
 * its timing, delivery and labels beside them. A failure comes first, with
 * what its category means.
 */
export function BackgroundTaskOverview({
  task,
  approvalPanel,
}: {
  task: BackgroundTaskDetail
  /** What answers the approval it waits for. */
  approvalPanel?: ApprovalPanelOf
}) {
  const person = usePersonName()
  const labels = Object.entries(task.labels ?? {})
  return (
    <SubmissionLayout
      aside={
        <>
          <AsideSection title="Timing">
            <AsideFact label="Created">
              {formatDateTime(task.created_at)}
            </AsideFact>
            <AsideFact label="Started">
              {task.started_at ? formatDateTime(task.started_at) : "Not yet"}
            </AsideFact>
            {task.ended_at && (
              <AsideFact label="Ended">
                {formatDateTime(task.ended_at)}
              </AsideFact>
            )}
            <AsideFact label="Duration">{duration(task.duration_ms)}</AsideFact>
            <AsideFact label="Attempts">{task.attempts}</AsideFact>
          </AsideSection>
          {task.delivery && (
            <AsideSection title="Delivery">
              <AsideFact label="Queue">{task.delivery.queue}</AsideFact>
              <AsideFact label="Key">
                <code className="text-xs">{task.delivery.key}</code>
              </AsideFact>
              <AsideFact label="Enqueued">
                {formatDateTime(task.delivery.enqueued_at)}
              </AsideFact>
            </AsideSection>
          )}
          {labels.length > 0 && (
            <AsideSection title="Labels">
              {labels.map(([name, value]) => (
                <AsideFact key={name} label={humanize(name)}>
                  {value}
                </AsideFact>
              ))}
            </AsideSection>
          )}
        </>
      }
    >
      <StatusNotice
        task={task}
        approvalPanel={approvalPanel}
      />
      <PageSection
        title="What it was asked to do"
        meta={
          <CopyButton
            value={JSON.stringify(task.payload, null, 2)}
            label="Copy input"
            size="xs"
          />
        }
        description="Its input, as the worker received it."
      >
        <JsonView value={task.payload} className="max-h-[28rem]" />
      </PageSection>
      {task.result && <ResultSection result={task.result} />}
      <PageSection title="Record">
        <RecordList>
          <RecordRow label="Task ID">
            <code>{task.id}</code>
          </RecordRow>
          <RecordRow label="Job">
            <code>{task.job_name}</code>
          </RecordRow>
          <RecordRow label="Requested by">
            {actorLabel(task.requested_by, person) ?? "Unknown"}
          </RecordRow>
          {task.correlation_id && (
            <RecordRow label="Correlation ID">
              <code>{task.correlation_id}</code>
            </RecordRow>
          )}
          <RecordRow label="Last updated">
            {formatDateTime(task.updated_at)}
          </RecordRow>
        </RecordList>
      </PageSection>
    </SubmissionLayout>
  )
}

/* -------------------------------------------------------------------------- */
/* Attempts                                                                   */
/* -------------------------------------------------------------------------- */

function FailureList({ failures }: { failures: BackgroundTaskRunFailure[] }) {
  return (
    <div className="mt-6">
      <h3 className="mb-2.5 text-sm font-medium">
        {failures.length === 1 ? "Failure" : `Failures (${failures.length})`}
      </h3>
      <ul className="flex flex-col divide-y rounded-(--radius-card) border">
        {failures.map((failure, index) => {
          const category = failureCategory(failure.category)
          return (
            <li key={index} className="flex flex-col gap-2 px-4 py-3.5">
              <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1.5">
                <Chip tone={category.tone} title={category.description}>
                  {category.label}
                </Chip>
                <code className="text-xs wrap-anywhere text-muted-foreground">
                  {failure.type}
                </code>
                {failure.step && (
                  <span className="text-xs text-muted-foreground">
                    in step {failure.step}
                  </span>
                )}
                <time
                  dateTime={failure.occurred_at}
                  className="ml-auto text-2xs text-subtle"
                >
                  {formatDateTime(failure.occurred_at)}
                </time>
              </div>
              <p className="text-sm wrap-anywhere">{failure.message}</p>
              {failure.stack_trace && (
                <Disclosure summary="Stack trace">
                  <pre>{failure.stack_trace}</pre>
                </Disclosure>
              )}
              {failure.cause_chain?.length > 0 && (
                <Disclosure
                  summary={`Caused by (${failure.cause_chain.length})`}
                >
                  <pre>{failure.cause_chain.join("\n\n")}</pre>
                </Disclosure>
              )}
            </li>
          )
        })}
      </ul>
    </div>
  )
}

function StepList({ steps }: { steps: BackgroundTaskStep[] }) {
  return (
    <div className="mt-6">
      <h3 className="text-sm font-medium">Steps</h3>
      <ExecutionPlan className="mt-4">
        {steps.map((step, index) => (
          <ExecutionStep
            key={`${step.name}-${index}`}
            step={String(index + 1).padStart(2, "0")}
            name={step.name}
            completed={step.status === "COMPLETED"}
            status={`${statusName(step.status)}${step.attempt > 1 ? ` · try ${step.attempt}` : ""}`}
            detail={
              [
                step.started_at && `Started ${formatDateTime(step.started_at)}`,
                step.duration_ms !== null && formatDuration(step.duration_ms),
              ]
                .filter(Boolean)
                .join(" · ") || undefined
            }
          />
        ))}
      </ExecutionPlan>
    </div>
  )
}

function Attempt({
  run,
  latest,
  runs,
  events,
  person,
}: {
  run: BackgroundTaskRun
  latest: boolean
  /** Every attempt, by ID, to name the one this restarted. */
  runs: Map<string, BackgroundTaskRun>
  events: BackgroundTaskEvent[]
  person: PersonName
}) {
  const shown = statusDisplay(run.status)
  const heading = React.useId()
  const restarted = run.restart_of ? runs.get(run.restart_of) : undefined
  // A restart keeps its original requester; who restarted it is its event's.
  const restart = restartEventOf(run, events)
  const restartedBy = actorLabel(restart?.actor, person)
  const exit =
    run.exit_description || (run.exit_code && run.exit_code !== run.status)
  return (
    <li
      aria-labelledby={heading}
      className="border-t py-7 first:border-t-0 first:pt-0"
    >
      <div className="mb-4 flex flex-wrap items-center gap-x-3 gap-y-2">
        <h2 id={heading} className="text-lg font-[550]">
          Attempt {run.attempt}
        </h2>
        <StatusBadge status={shown.status}>{shown.label}</StatusBadge>
        {latest && <Chip tone="outline">Latest</Chip>}
      </div>
      <RecordList>
        <RecordRow label="Started">
          {run.started_at ? formatDateTime(run.started_at) : "Not yet"}
        </RecordRow>
        {run.ended_at && (
          <RecordRow label="Ended">{formatDateTime(run.ended_at)}</RecordRow>
        )}
        <RecordRow label="Duration">{duration(run.duration_ms)}</RecordRow>
        <RecordRow label="Requested by">
          {actorLabel(run.requested_by, person) ?? "Unknown"}
        </RecordRow>
        {run.restart_of && (
          <RecordRow label="Restarted">
            {restarted
              ? `From attempt ${restarted.attempt}`
              : "From an earlier attempt"}
            {restartedBy && ` · by ${restartedBy}`}
            {restart && ` · ${formatDateTime(restart.at)}`}
          </RecordRow>
        )}
        {exit && (
          <RecordRow label="Exit">
            <code>{run.exit_code}</code>
            {run.exit_description && ` · ${run.exit_description}`}
          </RecordRow>
        )}
        <RecordRow label="Attempt ID">
          <code>{run.id}</code>
        </RecordRow>
      </RecordList>
      {run.failures.length > 0 && <FailureList failures={run.failures} />}
      {run.steps.length > 0 && <StepList steps={run.steps} />}
    </li>
  )
}

/**
 * The Attempts tab: each run of the task, newest first, with who asked,
 * how long it took, its failures (stack traces folded away) and its steps.
 */
export function BackgroundTaskAttempts({
  task,
}: {
  task: BackgroundTaskDetail
}) {
  const person = usePersonName()
  const runs = React.useMemo(
    () => new Map(task.runs.map((run) => [run.id, run])),
    [task.runs]
  )
  return (
    <div className={BODY}>
      {task.runs.length === 0 ? (
        <PanelEmpty illustration="waiting">
          No attempt has started yet. It shows here once a worker picks the task
          up.
        </PanelEmpty>
      ) : (
        <ol aria-label="Attempts" className="flex list-none flex-col p-0">
          {task.runs.map((run, index) => (
            <Attempt
              key={run.id}
              run={run}
              latest={index === 0}
              runs={runs}
              events={task.events}
              person={person}
            />
          ))}
        </ol>
      )}
    </div>
  )
}

/* -------------------------------------------------------------------------- */
/* Activity                                                                   */
/* -------------------------------------------------------------------------- */

/** An event's details on one line: attempt, status change, step, who, what. */
function describeEvent(
  event: BackgroundTaskEvent,
  attempts: Map<string, number>,
  person: PersonName,
  showStep: boolean
) {
  const attempt = attempts.get(event.run_id)
  const change =
    event.from_status && event.to_status
      ? `${statusName(event.from_status)} → ${statusName(event.to_status)}`
      : event.to_status
        ? `Now ${statusName(event.to_status).toLowerCase()}`
        : undefined
  // The worker's own bookkeeping needs no name; people and services do.
  const actor =
    event.actor && event.actor.kind !== "SYSTEM"
      ? actorLabel(event.actor, person)
      : undefined
  return (
    [
      attempt !== undefined && `Attempt ${attempt}`,
      change,
      showStep && event.step && `step ${event.step}`,
      actor && `by ${actor}`,
      event.message,
    ]
      .filter(Boolean)
      .join(" · ") || undefined
  )
}

/**
 * The Activity tab: the task's audit trail, newest first: each attempt
 * created and started, status changes, steps, retries, restarts and who
 * made them.
 */
export function BackgroundTaskActivity({
  task,
}: {
  task: BackgroundTaskDetail
}) {
  const person = usePersonName()
  const attempts = React.useMemo(
    () => new Map(task.runs.map((run) => [run.id, run.attempt])),
    [task.runs]
  )
  const events = React.useMemo(() => [...task.events].reverse(), [task.events])
  // A job that is one step (every job today: "run") says nothing by naming
  // it; what the job is doing, e.g. a workflow's step, is in the message.
  const showStep = React.useMemo(
    () =>
      new Set(task.events.map((event) => event.step).filter(Boolean)).size > 1,
    [task.events]
  )
  return (
    <div className={BODY}>
      {events.length === 0 ? (
        <PanelEmpty illustration="activity">
          Nothing is recorded yet.
        </PanelEmpty>
      ) : (
        <>
          <p className="mb-6 text-[0.8125rem] text-muted-foreground">
            Everything recorded about this task, newest first.
          </p>
          <EventList aria-label="Activity" className="max-w-[75ch]">
            {events.map((event) => (
              <EventItem
                key={`${event.run_id}:${event.sequence}`}
                type={eventTitle(event)}
                time={formatDateTime(event.at)}
                error={isFailureEvent(event)}
              >
                {describeEvent(event, attempts, person, showStep)}
              </EventItem>
            ))}
          </EventList>
        </>
      )}
    </div>
  )
}
