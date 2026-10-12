import * as React from "react"
import { cn } from "cn"

import { KindGlyph } from "@/features/builder/components/glyph"
import { PanelEmpty } from "@/components/forge/empty-state"
import { ErrorCallout } from "@/components/forge/feedback"
import { Icon } from "@/components/forge/icon"
import { Chip } from "@/components/forge/status"
import { Disclosure, RecordList, RecordRow } from "@/components/forge/task-page"
import { CopyButton, JsonView } from "@/features/json/components/json-view"
import { ValueTree } from "@/features/json/components/value-tree"
import { Button } from "@/components/ui/button"
import {
  InputGroup,
  InputGroupAddon,
  InputGroupInput,
} from "@/components/ui/input-group"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Skeleton } from "@/components/ui/skeleton"
import { Switch } from "@/components/ui/switch"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import type { AgentDocument } from "@/features/adk-workflows/lib/document"
import { AGENT_KINDS } from "@/features/adk-workflows/lib/model"
import { downloadBlob } from "@/features/builder/components/utils"
import {
  downloadAdkRunFile,
  failureTitle,
  shortId,
  stepKindOf,
  stepNameOf,
  type AdkRunDetail,
} from "@/features/runs/lib/runs"
import {
  eventKinds,
  eventSummary,
  gapMs,
  isPrivateKey,
  matchesTrace,
  preview,
  stateAt,
  stateChanges,
  stateKeys,
  traceTotals,
  TRACE_KINDS,
  useAdkRunTrace,
  type AdkRunTrace,
  type StateChange,
  type TraceEvent,
  type TraceKind,
} from "@/features/runs/lib/trace"
import { toApiError } from "@/lib/api/index"
import { formatDateTime, formatDuration } from "@/lib/format"
import { toast } from "@/components/ui/toast"

/*
 * The Trace tab: everything a run did in its ADK session, for the
 * organization's admins. Three views: the timeline (every event, filtered by
 * step, kind or text, one open in the inspector beside it with the state as
 * it left it), the state (each key, its value before and after the run, and
 * every change to it) and the worker's record of the run.
 */

type TraceView = "timeline" | "state" | "worker"

const VIEWS: Record<TraceView, string> = {
  timeline: "Timeline",
  state: "State",
  worker: "Worker record",
}

// The run's events and the inspector beside them scroll on their own.
const PANE = "max-h-[min(78vh,980px)] overflow-auto"

/** "+0.012s", "+4.2s", "+3m 2s": how long after the run's first event. */
function offset(ms: number | undefined) {
  if (ms === undefined || ms < 0) return ""
  if (ms < 10_000) return `+${(ms / 1000).toFixed(ms < 1000 ? 3 : 2)}s`
  return `+${formatDuration(ms)}`
}

const json = (value: unknown) => JSON.stringify(value ?? null, null, 2)

/** A step's name and glyph, as the run's workflow has it. */
function useStepLabel(doc: AgentDocument | undefined, root = "Run") {
  return React.useCallback(
    (id: string | null) => {
      if (!id) return { name: root, kind: undefined }
      const kind = stepKindOf({ id, kind: "" }, doc)
      const name = stepNameOf({ id, name: "", kind: kind ?? "" }, doc)
      return { name, kind }
    },
    [doc, root]
  )
}

/* -------------------------------------------------------------------------- */
/* Small pieces                                                               */
/* -------------------------------------------------------------------------- */

function KindChips({ kinds }: { kinds: TraceKind[] }) {
  return (
    <>
      {kinds.map((kind) => (
        <Chip
          key={kind}
          tone={TRACE_KINDS[kind].tone}
          className="h-[18px] px-1.5 text-3xs"
        >
          {TRACE_KINDS[kind].label}
        </Chip>
      ))}
    </>
  )
}

/** A titled part of the inspector. */
function Part({
  title,
  meta,
  children,
}: {
  title: React.ReactNode
  meta?: React.ReactNode
  children: React.ReactNode
}) {
  return (
    <section className="flex flex-col gap-2 border-t pt-4 first:border-t-0 first:pt-0">
      <div className="flex items-center justify-between gap-3">
        <h3 className="text-xs font-[550] text-foreground">{title}</h3>
        {meta && <span className="text-2xs text-muted-foreground">{meta}</span>}
      </div>
      {children}
    </section>
  )
}

/** One small switch of options: the view, a state's moment. */
function Switcher<T extends string>({
  label,
  options,
  value,
  onValueChange,
}: {
  label: string
  options: Record<T, string>
  value: T
  onValueChange: (value: T) => void
}) {
  return (
    <ToggleGroup
      aria-label={label}
      value={[value]}
      onValueChange={(next) => {
        if (next[0]) onValueChange(next[0] as T)
      }}
      spacing={0}
      className="rounded-(--radius-control) bg-muted p-[3px]"
    >
      {(Object.keys(options) as T[]).map((key) => (
        <ToggleGroupItem
          key={key}
          value={key}
          className="h-[26px] min-w-0 rounded-(--radius-soft)! px-2.5 text-xs font-normal text-muted-foreground hover:bg-transparent hover:text-foreground aria-pressed:bg-background aria-pressed:text-foreground aria-pressed:shadow-(--shadow-raised)"
        >
          {options[key]}
        </ToggleGroupItem>
      ))}
    </ToggleGroup>
  )
}

/** What the run's events add up to, in one line of facts. */
function Totals({ trace }: { trace: AdkRunTrace }) {
  const totals = React.useMemo(() => traceTotals(trace.events), [trace.events])
  const facts: [string, React.ReactNode][] = [
    ["Events", totals.events.toLocaleString()],
    ["State changes", totals.stateChanges.toLocaleString()],
    ["Tool calls", totals.calls.toLocaleString()],
    ["Model turns", totals.modelTurns.toLocaleString()],
    [
      "Tokens",
      totals.totalTokens
        ? `${totals.totalTokens.toLocaleString()} (${totals.promptTokens.toLocaleString()} in · ${totals.outputTokens.toLocaleString()} out)`
        : "—",
    ],
    ["Errors", totals.errors.toLocaleString()],
  ]
  return (
    <dl className="mb-5 grid grid-cols-6 rounded-(--radius-card) border @max-[1050px]/shell:grid-cols-3 @max-[600px]/shell:grid-cols-2">
      {facts.map(([label, value]) => (
        <div
          key={label}
          className="border-r px-3.5 py-2.5 last:border-r-0 @max-[1050px]/shell:border-b @max-[1050px]/shell:nth-[3n]:border-r-0"
        >
          <dt className="text-2xs text-muted-foreground">{label}</dt>
          <dd
            className={cn(
              "mt-1 text-sm tabular-nums",
              label === "Errors" && totals.errors > 0 && "text-destructive"
            )}
          >
            {value}
          </dd>
        </div>
      ))}
    </dl>
  )
}

function Note({ children }: { children: React.ReactNode }) {
  return (
    <p
      role="note"
      className="mb-4 flex items-center gap-3 rounded-(--radius-card) border bg-notice-surface px-3 py-2 text-xs text-notice-foreground"
    >
      <Icon icon="info" size={14} className="shrink-0 text-notice-accent" />
      <span>{children}</span>
    </p>
  )
}

/* -------------------------------------------------------------------------- */
/* Timeline                                                                   */
/* -------------------------------------------------------------------------- */

type Label = ReturnType<ReturnType<typeof useStepLabel>>

function EventRow({
  event,
  selected,
  start,
  onSelect,
}: {
  event: TraceEvent
  selected: boolean
  start: string | undefined
  onSelect: () => void
}) {
  const kinds = eventKinds(event)
  return (
    <li>
      <button
        type="button"
        data-seq={event.seq}
        aria-current={selected || undefined}
        onClick={onSelect}
        className={cn(
          "flex w-full min-w-0 flex-col gap-1 rounded-(--radius-item) px-2.5 py-2 text-left hover:bg-accent/60",
          selected && "bg-accent hover:bg-accent",
          event.error && "border-l-2 border-destructive"
        )}
      >
        <span className="flex min-w-0 items-center gap-2">
          <code className="w-9 shrink-0 text-3xs text-subtle tabular-nums">
            #{event.seq}
          </code>
          <span className="min-w-0 truncate text-xs font-medium text-foreground">
            {event.author}
          </span>
          {!event.own && event.step && (
            <span className="shrink-0 text-3xs text-subtle">inside</span>
          )}
          <span className="ml-auto shrink-0 font-mono text-3xs text-subtle tabular-nums">
            {offset(gapMs(start, event.at))}
          </span>
        </span>
        <span className="flex min-w-0 items-center gap-2 pl-11">
          <span
            className={cn(
              "min-w-0 truncate text-2xs",
              event.error ? "text-destructive" : "text-muted-foreground"
            )}
          >
            {eventSummary(event)}
          </span>
        </span>
        {kinds.length > 0 && (
          <span className="flex flex-wrap gap-1 pl-11">
            <KindChips kinds={kinds} />
          </span>
        )}
      </button>
    </li>
  )
}

/** The events, each step's together, with a heading where the step changes. */
function EventListPane({
  events,
  selected,
  start,
  label,
  onSelect,
}: {
  events: TraceEvent[]
  selected: number | undefined
  start: string | undefined
  label: (id: string | null) => Label
  onSelect: (seq: number) => void
}) {
  const list = React.useRef<HTMLDivElement>(null)

  // Arrow keys move through the events.
  const onKeyDown = (key: React.KeyboardEvent) => {
    if (key.key !== "ArrowDown" && key.key !== "ArrowUp") return
    key.preventDefault()
    const at = events.findIndex((event) => event.seq === selected)
    const next =
      events[
        Math.min(
          events.length - 1,
          Math.max(0, at + (key.key === "ArrowDown" ? 1 : -1))
        )
      ]
    if (next) onSelect(next.seq)
  }

  // Keep the selected event in view, scrolling only the list (not the page).
  React.useEffect(() => {
    const box = list.current
    const row = box?.querySelector(`[data-seq="${selected}"]`)
    if (!box || !row) return
    const shown = box.getBoundingClientRect()
    const at = row.getBoundingClientRect()
    // Room for the step's sticky heading above it.
    const top = shown.top + 36
    if (at.top < top) box.scrollTop -= top - at.top
    else if (at.bottom > shown.bottom) box.scrollTop += at.bottom - shown.bottom
  }, [selected])

  if (!events.length)
    return (
      <PanelEmpty illustration="search">
        No event matches. Clear the filters to see them all.
      </PanelEmpty>
    )

  const rows: React.ReactNode[] = []
  let step: string | null | undefined
  for (const event of events) {
    if (event.step !== step) {
      step = event.step
      const shown = label(step)
      const info = shown.kind && AGENT_KINDS[shown.kind]
      rows.push(
        <li
          key={`step-${event.seq}`}
          className="sticky top-0 z-10 flex items-center gap-2 bg-background px-2.5 pt-3 pb-1.5 text-2xs font-medium text-muted-foreground"
        >
          {info && <KindGlyph info={info} size="sm" />}
          <span className="truncate">{shown.name}</span>
          {step && <code className="text-3xs text-subtle">{step}</code>}
        </li>
      )
    }
    rows.push(
      <EventRow
        key={event.seq}
        event={event}
        selected={event.seq === selected}
        start={start}
        onSelect={() => onSelect(event.seq)}
      />
    )
  }
  return (
    <div
      ref={list}
      tabIndex={0}
      onKeyDown={onKeyDown}
      aria-label="Events"
      className={cn(PANE, "rounded-(--radius-card) border p-1")}
    >
      <ul className="flex flex-col gap-0.5">{rows}</ul>
    </div>
  )
}

/** A state key's change in an event: before and after. */
function ChangeRow({
  change,
  quiet,
}: {
  change: Pick<StateChange, "key" | "existed" | "before" | "after">
  quiet?: boolean
}) {
  // Set again to what it held: nothing changed.
  const same =
    change.existed &&
    JSON.stringify(change.before) === JSON.stringify(change.after)
  return (
    <div className="flex flex-col gap-1.5 rounded-(--radius-control) border px-2.5 py-2">
      <div className="flex min-w-0 items-center gap-2">
        <code
          className={cn(
            "min-w-0 truncate text-xs",
            quiet ? "text-subtle" : "text-foreground"
          )}
        >
          {change.key}
        </code>
        <Chip
          tone={!change.existed ? "success" : same ? "outline" : "warning"}
          className="ml-auto h-[18px] px-1.5 text-3xs"
        >
          {!change.existed ? "Added" : same ? "Same value" : "Changed"}
        </Chip>
      </div>
      <div className="grid grid-cols-2 gap-2 @max-[800px]/shell:grid-cols-1">
        <div className="min-w-0">
          <span className="text-3xs text-subtle">Before</span>
          {change.existed ? (
            <ValueTree value={change.before} className="mt-1 max-h-60" />
          ) : (
            <p className="mt-1 text-2xs text-subtle">Not set</p>
          )}
        </div>
        <div className="min-w-0">
          <span className="text-3xs text-subtle">After</span>
          <ValueTree value={change.after} className="mt-1 max-h-60" />
        </div>
      </div>
    </div>
  )
}

/** Everything one event said and did, with the state as it left it. */
function Inspector({
  trace,
  event,
  previous,
  start,
  label,
  onFilterStep,
  onDownload,
  subject = "run",
}: {
  trace: AdkRunTrace
  event: TraceEvent
  previous: TraceEvent | undefined
  start: string | undefined
  label: (id: string | null) => Label
  onFilterStep: (step: string) => void
  /** Download a version of a file the run saved; none where files aren't kept. */
  onDownload?: (name: string, version: number) => void
  /** What an event at no step is part of: "run", "conversation". */
  subject?: string
}) {
  const before = React.useMemo(
    () => stateAt(trace, event.seq - 1),
    [trace, event.seq]
  )
  const after = React.useMemo(
    () => stateAt(trace, event.seq),
    [trace, event.seq]
  )
  const changed = React.useMemo(
    () => new Set(Object.keys(event.state_delta)),
    [event.state_delta]
  )
  const step = label(event.step)
  const answered = new Map(
    trace.events.flatMap((each) =>
      each.responses.map((response) => [response.id, each.seq] as const)
    )
  )
  const askedBy = new Map(
    trace.events.flatMap((each) =>
      each.calls.map((call) => [call.id, each.seq] as const)
    )
  )
  const usage = event.usage

  return (
    <div
      className={cn(
        PANE,
        "flex flex-col gap-4 rounded-(--radius-card) border p-4"
      )}
      aria-label={`Event ${event.seq}`}
    >
      <div className="flex flex-col gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <h3 className="text-sm font-[550]">
            #{event.seq} · {event.author}
          </h3>
          <KindChips kinds={eventKinds(event)} />
          <CopyButton
            value={json(event)}
            label="Copy event"
            size="xs"
            className="ml-auto"
          />
        </div>
        <RecordList>
          <RecordRow label="Step">
            {event.step ? (
              <span className="flex flex-wrap items-center gap-2">
                {step.name}
                <code className="text-muted-foreground">{event.step}</code>
                {!event.own && (
                  <span className="text-muted-foreground">
                    (an event inside it)
                  </span>
                )}
                <Button
                  variant="link"
                  size="xs"
                  className="h-auto p-0"
                  onClick={() => onFilterStep(event.step!)}
                >
                  Only this step
                </Button>
              </span>
            ) : (
              <span className="text-muted-foreground">
                The {subject} itself (no step)
              </span>
            )}
          </RecordRow>
          <RecordRow label="When">
            {formatDateTime(event.at)}{" "}
            <span className="text-muted-foreground">
              {offset(gapMs(start, event.at))}
              {previous &&
                ` · ${formatDuration(gapMs(previous.at, event.at) ?? 0)} after #${previous.seq}`}
            </span>
          </RecordRow>
          <RecordRow label="Node path">
            <code>{event.path || "—"}</code>
          </RecordRow>
          {event.branch && (
            <RecordRow label="Branch">
              <code>{event.branch}</code>
            </RecordRow>
          )}
          {event.model && <RecordRow label="Model">{event.model}</RecordRow>}
          <RecordRow label="Event ID">
            <code>{event.id}</code>
          </RecordRow>
        </RecordList>
      </div>

      {event.error && (
        <Part title="Error">
          <ErrorCallout title={event.error.code || "It failed"}>
            {event.error.message || "No message was recorded."}
          </ErrorCallout>
        </Part>
      )}

      {event.text && (
        <Part
          title={event.role === "user" ? "Message in" : "Message"}
          meta={<CopyButton value={event.text} label="Copy" size="xs" />}
        >
          <pre className="max-h-[22rem] overflow-auto rounded-(--radius-control) border bg-muted p-3 text-xs/[1.65] wrap-anywhere whitespace-pre-wrap text-foreground">
            {event.text}
          </pre>
        </Part>
      )}

      {event.thought && (
        <Part title="Model's thoughts">
          <Disclosure summary="Show its thoughts">
            <pre>{event.thought}</pre>
          </Disclosure>
        </Part>
      )}

      {event.calls.length > 0 && (
        <Part
          title={
            event.calls.length === 1 ? "Call" : `${event.calls.length} calls`
          }
        >
          {event.calls.map((call, index) => {
            const answer = call.id ? answered.get(call.id) : undefined
            return (
              <div
                key={call.id ?? index}
                className="flex flex-col gap-1.5 rounded-(--radius-control) border px-2.5 py-2"
              >
                <div className="flex min-w-0 items-center gap-2">
                  <code className="min-w-0 truncate text-xs text-foreground">
                    {call.name}
                  </code>
                  {event.long_running.includes(call.id ?? "") && (
                    <Chip tone="warning" className="h-[18px] px-1.5 text-3xs">
                      Long-running
                    </Chip>
                  )}
                  <span className="ml-auto text-3xs text-subtle">
                    {answer ? `Answered in #${answer}` : "No answer yet"}
                  </span>
                </div>
                {call.id && (
                  <code className="text-3xs text-subtle">{call.id}</code>
                )}
                <ValueTree
                  value={call.args}
                  openDepth={2}
                  className="max-h-72"
                />
              </div>
            )
          })}
        </Part>
      )}

      {event.responses.length > 0 && (
        <Part
          title={
            event.responses.length === 1
              ? "Answer"
              : `${event.responses.length} answers`
          }
        >
          {event.responses.map((response, index) => {
            const asked = response.id ? askedBy.get(response.id) : undefined
            return (
              <div
                key={response.id ?? index}
                className="flex flex-col gap-1.5 rounded-(--radius-control) border px-2.5 py-2"
              >
                <div className="flex min-w-0 items-center gap-2">
                  <code className="min-w-0 truncate text-xs text-foreground">
                    {response.name}
                  </code>
                  {asked && (
                    <span className="ml-auto text-3xs text-subtle">
                      To the call in #{asked}
                    </span>
                  )}
                </div>
                <ValueTree
                  value={response.response}
                  openDepth={2}
                  className="max-h-72"
                />
              </div>
            )
          })}
        </Part>
      )}

      {event.has_output && (
        <Part
          title="Output"
          meta={
            <CopyButton value={json(event.output)} label="Copy" size="xs" />
          }
        >
          <ValueTree value={event.output} openDepth={2} className="max-h-80" />
          {event.output_for.length > 0 && (
            <p className="text-2xs text-muted-foreground">
              Handed on for{" "}
              {event.output_for.map((path) => (
                <code key={path} className="mr-1.5">
                  {path}
                </code>
              ))}
            </p>
          )}
        </Part>
      )}

      {changed.size > 0 && (
        <Part
          title="State changes"
          meta={`${changed.size} ${changed.size === 1 ? "key" : "keys"}`}
        >
          {[...changed].map((key) => (
            <ChangeRow
              key={key}
              quiet={isPrivateKey(key)}
              change={{
                key,
                existed: key in before,
                before: before[key],
                after: event.state_delta[key],
              }}
            />
          ))}
        </Part>
      )}

      {Object.keys(event.artifact_delta).length > 0 && (
        <Part title="Artifacts saved">
          <RecordList>
            {Object.entries(event.artifact_delta).map(([name, version]) => (
              <RecordRow key={name} label={<code>{name}</code>}>
                <span className="flex items-center gap-2">
                  Version {version + 1}
                  <span className="text-muted-foreground">
                    (ADK version {version})
                  </span>
                  {onDownload && (
                    <Button
                      variant="ghost"
                      size="icon-xs"
                      className="ml-auto"
                      aria-label={`Download ${name}, version ${version + 1}`}
                      onClick={() => onDownload(name, version)}
                    >
                      <Icon icon="download" />
                    </Button>
                  )}
                </span>
              </RecordRow>
            ))}
          </RecordList>
        </Part>
      )}

      {(event.route !== null && event.route !== undefined) ||
      event.transfer_to_agent ||
      event.escalate ? (
        <Part title="Where it went">
          <RecordList>
            {event.route !== null && event.route !== undefined && (
              <RecordRow label="Route">
                <code>{preview(event.route, 200)}</code>
              </RecordRow>
            )}
            {event.transfer_to_agent && (
              <RecordRow label="Handed off to">
                <code>{event.transfer_to_agent}</code>
              </RecordRow>
            )}
            {event.escalate && (
              <RecordRow label="Escalated">
                It escalated to its parent.
              </RecordRow>
            )}
          </RecordList>
        </Part>
      ) : null}

      {event.files.length > 0 && (
        <Part title="Files in the message">
          <RecordList>
            {event.files.map((file, index) => (
              <RecordRow key={index} label={file.name || `File ${index + 1}`}>
                {file.media_type}
                {file.size_bytes !== undefined &&
                  ` · ${file.size_bytes.toLocaleString()} bytes`}
                {file.uri && <code className="ml-1.5">{file.uri}</code>}
              </RecordRow>
            ))}
          </RecordList>
        </Part>
      )}

      {usage && (
        <Part title="Token usage">
          <RecordList>
            {(
              [
                ["Prompt", usage.prompt_tokens],
                ["Output", usage.output_tokens],
                ["Thinking", usage.thoughts_tokens],
                ["Cached", usage.cached_tokens],
                ["Tool prompt", usage.tool_prompt_tokens],
                ["Total", usage.total_tokens],
              ] as const
            )
              .filter(([, count]) => count !== null && count !== undefined)
              .map(([name, count]) => (
                <RecordRow key={name} label={name}>
                  <span className="tabular-nums">
                    {count!.toLocaleString()}
                  </span>
                </RecordRow>
              ))}
          </RecordList>
        </Part>
      )}

      {event.metadata && (
        <Part title="Forge's metadata">
          <ValueTree
            value={event.metadata}
            openDepth={2}
            className="max-h-60"
          />
        </Part>
      )}

      {(event.partial || event.interrupted) && (
        <Part title="Flags">
          <div className="flex gap-1.5">
            {event.partial && <Chip tone="outline">Partial</Chip>}
            {event.interrupted && <Chip tone="warning">Interrupted</Chip>}
          </div>
        </Part>
      )}

      <Part
        title="State after this event"
        meta={`${Object.keys(after).length} keys`}
      >
        <Disclosure summary="Show the whole state, its changes marked">
          <ValueTree
            value={after}
            highlight={changed}
            quiet={isPrivateKey}
            className="border-0 bg-transparent p-0"
          />
        </Disclosure>
      </Part>

      <Part title="Raw event">
        <Disclosure summary="Show it as JSON">
          <JsonView value={event} className="border-0 bg-transparent p-0" />
        </Disclosure>
      </Part>
    </div>
  )
}

function Timeline({
  trace,
  doc,
  step,
  onStepChange,
  selected,
  onSelect,
  onDownload,
  subject = "run",
}: {
  trace: AdkRunTrace
  doc: AgentDocument | undefined
  step: string | undefined
  onStepChange: (step: string | undefined) => void
  selected: number | undefined
  onSelect: (seq: number | undefined) => void
  onDownload?: (name: string, version: number) => void
  /** What the events are of: "run", "conversation". */
  subject?: string
}) {
  const label = useStepLabel(doc, subject[0].toUpperCase() + subject.slice(1))
  const [query, setQuery] = React.useState("")
  const [kinds, setKinds] = React.useState<TraceKind[]>([])
  const events = trace.events
  const start = events[0]?.at

  const shown = React.useMemo(
    () => events.filter((event) => matchesTrace(event, { step, kinds, query })),
    [events, step, kinds, query]
  )
  const present = React.useMemo(() => {
    const found = new Set(events.flatMap(eventKinds))
    return (Object.keys(TRACE_KINDS) as TraceKind[]).filter((kind) =>
      found.has(kind)
    )
  }, [events])
  const stepOptions = React.useMemo(() => {
    const ids = [...new Set(events.flatMap((event) => event.steps))]
    return [
      { value: "", label: "Every step" },
      ...ids.map((id) => ({ value: id, label: label(id).name })),
    ]
  }, [events, label])

  // Keep a selection: the one asked for, else the last shown (where a run stands).
  const current = shown.find((event) => event.seq === selected) ?? shown.at(-1)
  const index = current ? events.indexOf(current) : -1
  const filtered = Boolean(step || kinds.length || query.trim())

  return (
    <>
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <InputGroup className="h-7 w-[min(18rem,100%)]">
          <InputGroupInput
            aria-label="Search the events"
            placeholder="Search text, tools, values…"
            value={query}
            onChange={(change) => setQuery(change.target.value)}
            className="text-xs"
          />
          <InputGroupAddon align="inline-end">
            <Icon icon="search" />
          </InputGroupAddon>
        </InputGroup>
        {stepOptions.length > 1 && (
          <Select
            items={stepOptions}
            value={step ?? ""}
            onValueChange={(value) =>
              onStepChange(value ? String(value) : undefined)
            }
          >
            <SelectTrigger
              size="sm"
              aria-label="Step"
              className="max-w-56 text-xs"
            >
              <SelectValue />
            </SelectTrigger>
            <SelectContent alignItemWithTrigger={false}>
              {stepOptions.map((option) => (
                <SelectItem key={option.value} value={option.value}>
                  {option.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        )}
        <ToggleGroup
          aria-label="Kinds of event"
          multiple
          value={kinds}
          onValueChange={(next) => setKinds(next as TraceKind[])}
          spacing={1}
          className="flex-wrap"
        >
          {present.map((kind) => (
            <ToggleGroupItem
              key={kind}
              value={kind}
              className="h-6 min-w-0 rounded-(--radius-chip)! border px-2 text-2xs font-normal text-muted-foreground aria-pressed:border-foreground/30 aria-pressed:bg-accent aria-pressed:text-foreground"
            >
              {TRACE_KINDS[kind].label}
            </ToggleGroupItem>
          ))}
        </ToggleGroup>
        <span className="ml-auto text-2xs text-muted-foreground tabular-nums">
          {filtered
            ? `${shown.length.toLocaleString()} of ${events.length.toLocaleString()} events`
            : `${events.length.toLocaleString()} events`}
        </span>
        {filtered && (
          <Button
            variant="ghost"
            size="xs"
            onClick={() => {
              setQuery("")
              setKinds([])
              onStepChange(undefined)
            }}
          >
            Clear filters
          </Button>
        )}
      </div>
      <div className="grid grid-cols-[minmax(0,26rem)_minmax(0,1fr)] gap-5 @max-[1050px]/shell:grid-cols-1">
        <EventListPane
          events={shown}
          selected={current?.seq}
          start={start}
          label={label}
          onSelect={onSelect}
        />
        {current ? (
          <Inspector
            trace={trace}
            event={current}
            previous={index > 0 ? events[index - 1] : undefined}
            start={start}
            label={label}
            onFilterStep={(id) => onStepChange(id)}
            onDownload={onDownload}
            subject={subject}
          />
        ) : (
          <div />
        )}
      </div>
    </>
  )
}

/* -------------------------------------------------------------------------- */
/* State                                                                      */
/* -------------------------------------------------------------------------- */

type Moment = "before" | "after" | "now"

function StateView({
  trace,
  doc,
  onOpenEvent,
  subject = "run",
}: {
  trace: AdkRunTrace
  doc: AgentDocument | undefined
  onOpenEvent: (seq: number) => void
  /** What the state is of: "run", "conversation". */
  subject?: string
}) {
  const label = useStepLabel(doc, subject[0].toUpperCase() + subject.slice(1))
  const moments: Record<Moment, string> = {
    before: `Before the ${subject}`,
    after: `After the ${subject}`,
    now: "Session now",
  }
  const [showPrivate, setShowPrivate] = React.useState(false)
  const [query, setQuery] = React.useState("")
  const [moment, setMoment] = React.useState<Moment>("after")
  const keys = React.useMemo(() => stateKeys(trace), [trace])
  const changes = React.useMemo(() => stateChanges(trace), [trace])
  const shown = keys.filter(
    (entry) =>
      (showPrivate || !isPrivateKey(entry.key)) &&
      entry.key.toLowerCase().includes(query.trim().toLowerCase())
  )
  const [selected, setSelected] = React.useState<string>()
  const current = shown.find((entry) => entry.key === selected) ?? shown[0]
  const history = changes.filter((change) => change.key === current?.key)
  const hidden = keys.filter((entry) => isPrivateKey(entry.key)).length
  const whole =
    moment === "before"
      ? trace.state_before
      : moment === "after"
        ? trace.state_after
        : trace.state
  const touched = React.useMemo(
    () => new Set(changes.map((change) => change.key)),
    [changes]
  )

  return (
    <div className="flex flex-col gap-8">
      <div className="grid grid-cols-[minmax(0,20rem)_minmax(0,1fr)] gap-5 @max-[1050px]/shell:grid-cols-1">
        <div className="flex flex-col gap-2">
          <InputGroup className="h-7">
            <InputGroupInput
              aria-label="Find a key"
              placeholder="Find a key…"
              value={query}
              onChange={(change) => setQuery(change.target.value)}
              className="text-xs"
            />
            <InputGroupAddon align="inline-end">
              <Icon icon="search" />
            </InputGroupAddon>
          </InputGroup>
          {hidden > 0 && (
            <label className="flex items-center gap-2 text-2xs text-muted-foreground">
              <Switch
                checked={showPrivate}
                onCheckedChange={(checked) => setShowPrivate(Boolean(checked))}
              />
              Show Forge&apos;s own keys ({hidden})
            </label>
          )}
          <div className={cn(PANE, "rounded-(--radius-card) border p-1")}>
            {shown.length === 0 ? (
              <PanelEmpty illustration="search">
                {keys.length ? "No key matches." : "The state is empty."}
              </PanelEmpty>
            ) : (
              <ul className="flex flex-col gap-0.5">
                {shown.map((entry) => (
                  <li key={entry.key}>
                    <button
                      type="button"
                      aria-current={entry.key === current?.key || undefined}
                      onClick={() => setSelected(entry.key)}
                      className={cn(
                        "flex w-full min-w-0 items-center gap-2 rounded-(--radius-item) px-2.5 py-1.5 text-left hover:bg-accent/60",
                        entry.key === current?.key &&
                          "bg-accent hover:bg-accent"
                      )}
                    >
                      <code
                        className={cn(
                          "min-w-0 truncate text-xs",
                          isPrivateKey(entry.key)
                            ? "text-subtle"
                            : "text-foreground"
                        )}
                      >
                        {entry.key}
                      </code>
                      <span className="ml-auto flex shrink-0 gap-1">
                        {entry.added && (
                          <Chip
                            tone="success"
                            className="h-[18px] px-1.5 text-3xs"
                          >
                            Added
                          </Chip>
                        )}
                        <Chip
                          tone={entry.changes ? "warning" : "outline"}
                          className="h-[18px] px-1.5 text-3xs"
                        >
                          {entry.changes === 1
                            ? "1 change"
                            : `${entry.changes} changes`}
                        </Chip>
                      </span>
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>

        {current ? (
          <div
            className={cn(
              PANE,
              "flex flex-col gap-4 rounded-(--radius-card) border p-4"
            )}
          >
            <div className="flex flex-wrap items-center gap-2">
              <h3 className="min-w-0 truncate font-mono text-sm font-[550]">
                {current.key}
              </h3>
              <CopyButton
                value={json(trace.state_after[current.key])}
                label="Copy value"
                size="xs"
                className="ml-auto"
              />
            </div>
            <Part title="Its value after the run">
              {current.key in trace.state_after ? (
                <ValueTree
                  value={trace.state_after[current.key]}
                  openDepth={2}
                  className="max-h-80"
                />
              ) : (
                <p className="text-2xs text-muted-foreground">Not set.</p>
              )}
            </Part>
            <Part
              title="How the run changed it"
              meta={
                history.length
                  ? `${history.length} ${history.length === 1 ? "change" : "changes"}, oldest first`
                  : undefined
              }
            >
              {history.length === 0 ? (
                <p className="text-2xs text-muted-foreground">
                  The {subject} never changed it: it had this value before the{" "}
                  {subject} started.
                </p>
              ) : (
                <ol className="flex flex-col gap-3">
                  {history.map((change) => (
                    <li
                      key={`${change.seq}-${change.key}`}
                      className="flex flex-col gap-1.5"
                    >
                      <div className="flex flex-wrap items-center gap-2 text-2xs text-muted-foreground">
                        <Button
                          variant="link"
                          size="xs"
                          className="h-auto p-0 font-mono"
                          onClick={() => onOpenEvent(change.seq)}
                        >
                          #{change.seq}
                        </Button>
                        <span>{label(change.step).name}</span>
                        <span>· {change.author}</span>
                        <span className="ml-auto">
                          {formatDateTime(change.at)}
                        </span>
                      </div>
                      <ChangeRow
                        change={change}
                        quiet={isPrivateKey(change.key)}
                      />
                    </li>
                  ))}
                </ol>
              )}
            </Part>
          </div>
        ) : (
          <div />
        )}
      </div>

      <section className="flex flex-col gap-3">
        <div className="flex flex-wrap items-center gap-3">
          <h2 className="text-lg font-[550]">The whole state</h2>
          <Switcher
            label="When"
            options={moments}
            value={moment}
            onValueChange={setMoment}
          />
          <CopyButton
            value={json(whole)}
            label="Copy state"
            size="xs"
            className="ml-auto"
          />
        </div>
        <p className="max-w-[75ch] text-xs text-muted-foreground">
          {moment === "before"
            ? `The session's state as the ${subject}'s first event found it: what it started with, and what earlier ${subject}s in the session set.`
            : moment === "after"
              ? `The state once every one of the ${subject}'s events applied. Keys the ${subject} set are marked.`
              : trace.later_changes
                ? `The session's state now: a later ${subject} in the same session has changed it since.`
                : "The session's state now, as the database has it."}
        </p>
        <ValueTree
          value={whole}
          highlight={moment === "before" ? undefined : touched}
          quiet={isPrivateKey}
          className="max-h-[36rem]"
        />
      </section>
    </div>
  )
}

/* -------------------------------------------------------------------------- */
/* Worker record                                                              */
/* -------------------------------------------------------------------------- */

function WorkerView({ trace, run }: { trace: AdkRunTrace; run: AdkRunDetail }) {
  return (
    <div className="flex max-w-[75rem] flex-col gap-8">
      <section>
        <h2 className="mb-3 text-lg font-[550]">Where it ran</h2>
        <RecordList>
          <RecordRow label="ADK session">
            <code>{trace.session_id}</code>
          </RecordRow>
          <RecordRow label="Invocation">
            <code>{trace.invocation_id ?? "None yet"}</code>
          </RecordRow>
          {trace.earlier_sessions.length > 0 && (
            <RecordRow label="Earlier sessions">
              {trace.earlier_sessions.map((id) => (
                <code key={id} className="mr-2 block">
                  {id}
                </code>
              ))}
            </RecordRow>
          )}
          <RecordRow label="Attempt">{run.attempt}</RecordRow>
        </RecordList>
      </section>
      <section>
        <div className="mb-3 flex items-center gap-3">
          <h2 className="text-lg font-[550]">What the worker keeps</h2>
          <CopyButton
            value={json(trace.worker)}
            label="Copy"
            size="xs"
            className="ml-auto"
          />
        </div>
        <p className="mb-3 max-w-[75ch] text-xs text-muted-foreground">
          The worker&apos;s bookkeeping for the run: the session and invocation
          it runs in, the steps it noted, the tool calls it journals until their
          answers are saved, the answers it replays, and how often it restarted
          or recovered.
        </p>
        <ValueTree
          value={trace.worker}
          openDepth={1}
          className="max-h-[32rem]"
        />
      </section>
      {run.trigger && (
        <section>
          <h2 className="mb-3 text-lg font-[550]">What started it</h2>
          <ValueTree value={run.trigger} openDepth={2} className="max-h-80" />
        </section>
      )}
      <section>
        <div className="mb-3 flex items-center gap-3">
          <h2 className="text-lg font-[550]">The workflow as it ran</h2>
          {run.document != null && (
            <CopyButton
              value={json(run.document)}
              label="Copy"
              size="xs"
              className="ml-auto"
            />
          )}
        </div>
        {run.document != null ? (
          <ValueTree
            value={run.document}
            openDepth={1}
            className="max-h-[32rem]"
          />
        ) : (
          <p className="text-xs text-muted-foreground">
            The run kept no copy of its workflow.
          </p>
        )}
      </section>
    </div>
  )
}

/* -------------------------------------------------------------------------- */
/* The tab                                                                    */
/* -------------------------------------------------------------------------- */

/**
 * The Trace tab: the run's every event and its state, to explore. `step`
 * narrows the timeline to a step's events (the Steps tab opens it so).
 */
export function AdkRunTraceTab({
  organizationId,
  run,
  doc,
  step,
  onStepChange,
}: {
  organizationId: string
  run: AdkRunDetail
  doc: AgentDocument | undefined
  step: string | undefined
  onStepChange: (step: string | undefined) => void
}) {
  const [view, setView] = React.useState<TraceView>("timeline")
  const [sessionId, setSessionId] = React.useState<string>()
  const [selected, setSelected] = React.useState<number>()
  const trace = useAdkRunTrace(organizationId, run, { sessionId })
  const data = trace.data

  const download = async (name: string, version: number) => {
    try {
      downloadBlob(
        await downloadAdkRunFile(organizationId, run.id, name, version),
        name
      )
    } catch (error) {
      toast.add({
        title: `Couldn't download ${name}`,
        description: toApiError(error).message,
        type: "error",
      })
    }
  }

  const openEvent = (seq: number) => {
    onStepChange(undefined)
    setSelected(seq)
    setView("timeline")
  }

  if (trace.error && !data) {
    return (
      <ErrorCallout
        title={
          trace.error.status === 403
            ? "Only those who manage the organization's runs see their trace"
            : failureTitle(trace.error, "Couldn't load the run's trace")
        }
        action={
          trace.error.status === 403 ? undefined : (
            <Button
              variant="outline"
              size="sm"
              onClick={() => void trace.refetch()}
            >
              Retry
            </Button>
          )
        }
      >
        {trace.error.message}
      </ErrorCallout>
    )
  }
  if (!data) {
    return (
      <div className="flex flex-col gap-3" aria-busy="true">
        <Skeleton className="h-14 w-full" />
        <div className="grid grid-cols-[26rem_1fr] gap-5">
          <Skeleton className="h-80" />
          <Skeleton className="h-80" />
        </div>
      </div>
    )
  }

  // The session it runs in now is "", the earlier ones by ID.
  const sessionOptions = [
    { value: "", label: "Its session now" },
    ...data.earlier_sessions.map((id, index) => ({
      value: id,
      label: `Earlier session ${index + 1} · ${shortId(id)}`,
    })),
  ]

  return (
    <>
      <div className="mb-4 flex flex-wrap items-center gap-3">
        <Switcher
          label="Trace view"
          options={VIEWS}
          value={view}
          onValueChange={setView}
        />
        {data.earlier_sessions.length > 0 && (
          <Select
            items={sessionOptions}
            value={sessionId ?? ""}
            onValueChange={(value) => {
              setSelected(undefined)
              setSessionId(value ? String(value) : undefined)
            }}
          >
            <SelectTrigger size="sm" aria-label="Session" className="text-xs">
              <SelectValue />
            </SelectTrigger>
            <SelectContent alignItemWithTrigger={false}>
              {sessionOptions.map((option) => (
                <SelectItem key={option.value} value={option.value}>
                  {option.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        )}
        <Button
          variant="outline"
          size="sm"
          className="ml-auto"
          onClick={() =>
            downloadBlob(
              new Blob([json({ run: run.id, ...data })], {
                type: "application/json",
              }),
              `run-${shortId(run.id)}-trace.json`
            )
          }
        >
          <Icon icon="download" data-icon="inline-start" />
          Download trace
        </Button>
      </div>
      <p className="mb-4 max-w-[90ch] text-[0.8125rem] text-muted-foreground">
        Everything the run did in its ADK session, event by event: what each
        step was sent and said, the tools called and what they answered, what it
        handed on, and every change it made to the session&apos;s state.
      </p>
      {data.omitted > 0 && (
        <Note>
          The run&apos;s first {data.omitted.toLocaleString()} events
          aren&apos;t listed: the trace lists its latest. Their state changes
          are in the state before.
        </Note>
      )}
      {view === "worker" ? (
        <WorkerView trace={data} run={run} />
      ) : data.events.length === 0 ? (
        <PanelEmpty illustration="waiting">
          Nothing is in its session yet. Its events show here once a worker
          starts the run.
        </PanelEmpty>
      ) : (
        <>
          <Totals trace={data} />
          {view === "timeline" && (
            <Timeline
              trace={data}
              doc={doc}
              step={step}
              onStepChange={onStepChange}
              selected={selected}
              onSelect={setSelected}
              onDownload={(name, version) => void download(name, version)}
            />
          )}
          {view === "state" && (
            <StateView trace={data} doc={doc} onOpenEvent={openEvent} />
          )}
        </>
      )}
    </>
  )
}

export {
  StateView as TraceStateView,
  Timeline as TraceTimeline,
  Totals as TraceTotals,
}
