import * as React from "react"
import { Link } from "@tanstack/react-router"
import { cn } from "cn"

import { Footnote, Footnotes } from "@/components/forge/activity"
import { PageEmpty } from "@/components/forge/empty-state"
import { ErrorCallout } from "@/components/forge/feedback"
import { Icon } from "@/components/forge/icon"
import { Button } from "@/components/ui/button"
import {
  ActivityPanel,
  AttentionPanel,
  OutcomesPanel,
  PeoplePanel,
  StepsPanel,
} from "@/features/overview/components/activity-panels"
import {
  slotsBy,
  type SeriesSlot,
} from "@/features/overview/components/chart-kit"
import { Ledger } from "@/features/overview/components/ledger"
import {
  detailOf,
  ledgerRow,
  SCOPE_ROW,
  type LedgerRow,
} from "@/features/overview/lib/ledger"
import {
  Fact,
  OverviewSkeleton,
  PanelRow,
  ReadOutText,
  SplitSwitch,
} from "@/features/overview/components/panels"
import {
  ModelsPanel,
  SpendPanel,
  TokensPanel,
} from "@/features/overview/components/usage-panels"
import {
  ageInWords,
  compact,
  DAY,
  plural,
  sharedTicks,
  usd,
} from "@/features/overview/lib/format"
import {
  bySubject,
  emptyTotals,
  OVERVIEW_PERIODS,
  SUBJECT_KINDS,
  summarize,
  useOrganizationOverview,
  weeksBySubject,
  type OrganizationOverview as Overview,
  type OverviewPeriod,
  type OverviewScope,
  type SubjectKind,
} from "@/features/overview/lib/overview"
import type { UsageContext } from "@/features/overview/lib/series"
import { parseTimestamp } from "@/lib/timestamps"

/*
 * An organization's overview, for the people who run it: a sentence on the
 * period, its workflows and agents compared in a ledger, then its tokens
 * (the headline), models and spend, what ran and how it ended, who ran it,
 * what in it used the tokens, and what needs a person now. Selecting a row,
 * or a type, focuses everything below on it.
 */

type KindFilter = "all" | SubjectKind

const dateFormat = new Intl.DateTimeFormat(undefined, { dateStyle: "medium" })

export function OrganizationOverview({
  organizationId,
  organizationName,
  period,
  onOpenRun,
}: {
  organizationId: string
  organizationName: string
  period: OverviewPeriod
  /** Open a workflow run's page; without it, waiting runs are listed only. */
  onOpenRun?: (runId: string) => void
}) {
  const query = useOrganizationOverview(organizationId, period)
  const overview = query.data

  if (query.error && !overview) {
    return (
      <ErrorCallout
        title={`Couldn't load ${organizationName}'s overview`}
        action={
          <Button
            variant="outline"
            size="sm"
            onClick={() => void query.refetch()}
          >
            Retry
          </Button>
        }
      >
        {query.error.message}
      </ErrorCallout>
    )
  }
  if (!overview) return <OverviewSkeleton />
  if (overview.subjects.length === 0) {
    return (
      <PageEmpty
        illustration="activity"
        title="Nothing to report on yet"
        description={`The overview compares ${organizationName}'s workflows and agents: how often they run, how it ends, the model tokens they use and what they cost. Build a workflow or an agent and run it, and it shows here.`}
      />
    )
  }
  return (
    <Report
      key={overview.period}
      organizationId={organizationId}
      overview={overview}
      // A new period loads behind the last one; fade it while it does.
      stale={query.isPlaceholderData}
      onOpenRun={onOpenRun}
    />
  )
}

function Report({
  organizationId,
  overview,
  stale,
  onOpenRun,
}: {
  organizationId: string
  overview: Overview
  stale: boolean
  onOpenRun?: (runId: string) => void
}) {
  const [kind, setKind] = React.useState<KindFilter>("all")
  const [focus, setFocus] = React.useState<string | null>(null)
  const now = parseTimestamp(overview.as_of).getTime()
  const subjects = React.useMemo(
    () => new Map(overview.subjects.map((subject) => [subject.key, subject])),
    [overview.subjects]
  )
  const focused = focus ? subjects.get(focus) : undefined
  const focusKey = focused?.key
  const scope = React.useMemo(
    (): OverviewScope =>
      focusKey
        ? { level: "subject", key: focusKey }
        : kind === "all"
          ? { level: "organization" }
          : { level: "kind", kind },
    [focusKey, kind]
  )

  // Colour slots by tokens over the last 12 weeks, so each keeps its colour
  // whatever the period or the focus.
  const weeks = React.useMemo(
    () => weeksBySubject(overview, "tokens"),
    [overview]
  )
  const slots = React.useMemo(
    () =>
      slotsBy(
        overview.subjects,
        (subject) => subject.key,
        (subject) => (weeks.get(subject.key) ?? []).reduce((a, b) => a + b, 0),
        (subject) => subject.name
      ),
    [overview.subjects, weeks]
  )
  const summary = React.useMemo(
    () => summarize(overview, scope),
    [overview, scope]
  )
  const unit = overview.unit
  const ticks = sharedTicks(
    summary.buckets.map((bucket) => bucket.start),
    unit
  )
  const context: UsageContext = { summary, scope, unit, ticks, slots, subjects }
  const kinds = (["workflow", "agent", "assistant"] as SubjectKind[]).filter(
    (k) => overview.subjects.some((subject) => subject.kind === k)
  )

  const select = (id: string) => {
    setFocus(id === SCOPE_ROW || id === focus ? null : id)
  }
  const chooseKind = (next: KindFilter) => {
    setKind(next)
    if (focused && next !== "all" && focused.kind !== next) setFocus(null)
  }

  return (
    // Phones keep the last footnote clear of the assistant launcher.
    <div
      data-slot="organization-overview"
      className={cn(
        "flex flex-col transition-opacity @max-[600px]/shell:pb-16",
        stale && "opacity-60"
      )}
    >
      <ReadOut overview={overview} now={now} />

      {overview.truncated && (
        <ErrorCallout
          className="mb-5"
          icon="warning"
          title="Only the latest runs are counted"
        >
          This organization started more workflow runs in the period than the
          overview reads at once; the oldest are left out of the figures.
        </ErrorCallout>
      )}

      <OverviewLedger
        overview={overview}
        kind={kind}
        kinds={kinds}
        focus={focus}
        slots={slots}
        weeks={weeks}
        now={now}
        onKind={chooseKind}
        onSelect={select}
      />

      <section
        id="overview-scope"
        aria-labelledby="overview-scope-title"
        // A new scope fades in, so the change reads as one.
        key={`${scope.level}:${focus ?? kind}`}
        className="mt-10 animate-in duration-150 fade-in-0 @max-[600px]/shell:mt-8"
      >
        <header className="mb-4 flex flex-wrap items-center gap-x-3 gap-y-1">
          <Icon icon="dashboard" size={16} className="text-muted-foreground" />
          <h2 id="overview-scope-title" className="text-sm font-medium">
            {focused
              ? focused.name
              : kind === "all"
                ? "All workflows and agents"
                : `All ${SUBJECT_KINDS[kind].plural.toLowerCase()}`}
          </h2>
          <span className="text-xs text-muted-foreground">
            Last {OVERVIEW_PERIODS[overview.period].label}
          </span>
          {focused && (
            <span className="ml-auto flex items-center gap-1">
              <OpenSubject organizationId={organizationId} subject={focused} />
              <Button
                variant="ghost"
                size="xs"
                className="text-muted-foreground"
                onClick={() => setFocus(null)}
              >
                <Icon icon="close" data-icon="inline-start" />
                Back to all
              </Button>
            </span>
          )}
        </header>

        <PanelRow>
          <TokensPanel context={context} />
          <ModelsPanel context={context} />
        </PanelRow>
        <PanelRow>
          <ActivityPanel context={context} />
          <OutcomesPanel context={context} />
          <SpendPanel context={context} />
        </PanelRow>
        <PanelRow>
          <PeoplePanel overview={overview} context={context} />
          <StepsPanel overview={overview} context={context} />
          <AttentionPanel
            overview={overview}
            context={context}
            now={now}
            onOpenRun={onOpenRun}
          />
        </PanelRow>
      </section>

      <Footnotes className="mt-10 max-w-[680px]">
        <Footnote icon="coins">
          Tokens and spend are every model call the organization's workflows,
          agents and assistant made, counted in the period it was made. Input
          includes cached input, and output includes thinking. Spend is priced
          from each model's cost in the model provider configuration; calls on
          models without one aren't counted.
        </Footnote>
        <Footnote icon="activity">
          A workflow run counts in the period it started, by how it has ended so
          far. An agent's invocation is one turn of a conversation, from the
          API, a test in its builder or a workflow; the assistant counts when
          asked from the organization's pages.
        </Footnote>
        <Footnote icon="clock">
          Days are yours ({overview.time_zone}). Changes compare with the{" "}
          {OVERVIEW_PERIODS[overview.period].label} before. The small bars count
          each week's tokens over the last 12 weeks; the last is this week so
          far. Colours rank workflows and agents by tokens over those weeks, so
          each keeps its colour whatever the period.
        </Footnote>
        <Footnote icon="info">
          {overview.recording_since
            ? `Model usage has been recorded since ${dateFormat.format(parseTimestamp(overview.recording_since))}.`
            : "Model usage is recorded from the next model call on."}
        </Footnote>
      </Footnotes>
    </div>
  )
}

/* Read-out ---------------------------------------------------------------- */

/**
 * The period in two sentences: what ran and what it used and cost; then
 * what waits on people.
 */
function ReadOut({ overview, now }: { overview: Overview; now: number }) {
  const all = summarize(overview, { level: "organization" })
  const workflows = summarize(overview, { level: "kind", kind: "workflow" })
  const agents = summarize(overview, { level: "kind", kind: "agent" })
  const { current } = all
  const active = (kind: SubjectKind) =>
    new Set(
      overview.activity
        .filter(
          (fact) =>
            fact.bucket >= 0 &&
            fact.started > 0 &&
            fact.subject.startsWith(`${kind}:`)
        )
        .map((fact) => fact.subject)
    ).size
  const oldest = overview.waiting[0]
  const parts: React.ReactNode[] = []
  if (workflows.current.started > 0) {
    parts.push(
      <React.Fragment key="workflows">
        <Fact>{plural(active("workflow"), "workflow")}</Fact> ran{" "}
        <Fact>{plural(workflows.current.started, "time")}</Fact>
      </React.Fragment>
    )
  }
  if (agents.current.started > 0) {
    parts.push(
      <React.Fragment key="agents">
        <Fact>{plural(active("agent"), "agent")}</Fact> answered{" "}
        <Fact>{plural(agents.current.started, "invocation")}</Fact>
      </React.Fragment>
    )
  }
  return (
    <ReadOutText>
      In the last {OVERVIEW_PERIODS[overview.period].label}{" "}
      {parts.length === 0 ? (
        <>
          <Fact>nothing ran</Fact>
        </>
      ) : (
        parts.reduce<React.ReactNode[]>(
          (joined, part, index) =>
            index === 0 ? [part] : [...joined, " and ", part],
          []
        )
      )}
      {current.tokens > 0 && (
        <>
          , using <Fact>{compact(current.tokens)} tokens</Fact> over{" "}
          <Fact>{plural(current.calls, "model call")}</Fact>
          {current.cost > 0 && (
            <>
              {" "}
              for <Fact>{usd(current.cost)}</Fact>
            </>
          )}
        </>
      )}
      .{" "}
      {oldest ? (
        <>
          <Fact>{plural(overview.waiting.length, "run")}</Fact>{" "}
          {overview.waiting.length === 1 ? "waits" : "wait"} on people
          {overview.waiting.length > 1 ? ", the oldest for " : " for "}
          <Fact>
            {ageInWords(
              Math.max(
                0,
                now - parseTimestamp(oldest.since ?? overview.as_of).getTime()
              )
            )}
          </Fact>
          .
        </>
      ) : (
        <>
          <Fact>Nothing</Fact> waits on people.
        </>
      )}
    </ReadOutText>
  )
}

/* Ledger ------------------------------------------------------------------ */

const KIND_FILTERS: Record<KindFilter, string> = {
  all: "All",
  workflow: "Workflows",
  agent: "Agents",
  assistant: "Assistant",
}

function OverviewLedger({
  overview,
  kind,
  kinds,
  focus,
  slots,
  weeks,
  now,
  onKind,
  onSelect,
}: {
  overview: Overview
  kind: KindFilter
  kinds: SubjectKind[]
  focus: string | null
  slots: Map<string, SeriesSlot>
  weeks: Map<string, number[]>
  now: number
  onKind: (kind: KindFilter) => void
  onSelect: (id: string) => void
}) {
  const rows = React.useMemo((): LedgerRow[] => {
    const totals = bySubject(overview)
    const waiting = new Map<string, { count: number; overdue: number }>()
    for (const run of overview.waiting) {
      const found = waiting.get(run.subject) ?? { count: 0, overdue: 0 }
      found.count += 1
      if (run.since && now - parseTimestamp(run.since).getTime() >= DAY)
        found.overdue += 1
      waiting.set(run.subject, found)
    }
    const listed = overview.subjects.filter(
      (subject) => kind === "all" || subject.kind === kind
    )
    const length = overview.weeks.length
    const none = Array<number>(length).fill(0)
    // Rows share one scale, so their bars compare; the whole has its own.
    const peak = Math.max(
      1,
      ...listed.flatMap((subject) => weeks.get(subject.key) ?? [])
    )
    const scopeWeeks = none.map((_, index) =>
      listed.reduce(
        (sum, subject) => sum + (weeks.get(subject.key)?.[index] ?? 0),
        0
      )
    )
    const scope = summarize(
      overview,
      kind === "all" ? { level: "organization" } : { level: "kind", kind }
    )
    const scopeWaiting = listed.reduce(
      (sum, subject) => {
        const found = waiting.get(subject.key)
        return found
          ? {
              count: sum.count + found.count,
              overdue: sum.overdue + found.overdue,
            }
          : sum
      },
      { count: 0, overdue: 0 }
    )
    const rows = listed.map((subject) => {
      const found = totals.get(subject.key)
      const held = waiting.get(subject.key) ?? { count: 0, overdue: 0 }
      return ledgerRow(
        found?.current ?? emptyTotals(),
        found?.previous ?? emptyTotals(),
        {
          id: subject.key,
          name: subject.name,
          detail: detailOf(subject.kind, subject.current, subject.last_at, now),
          kind: subject.kind,
          total: false,
          selected: focus === subject.key,
          slot: slots.get(subject.key),
          weeks: weeks.get(subject.key) ?? none,
          peak,
          waiting: held.count,
          overdue: held.overdue,
        }
      )
    })
    // Busiest first: by tokens, then by runs.
    rows.sort(
      (a, b) =>
        b.tokens - a.tokens || b.runs - a.runs || a.name.localeCompare(b.name)
    )
    return [
      ledgerRow(scope.current, scope.previous, {
        id: SCOPE_ROW,
        name:
          kind === "all"
            ? "All workflows and agents"
            : `All ${SUBJECT_KINDS[kind].plural.toLowerCase()}`,
        detail: `${plural(listed.length, kind === "all" ? "workflow or agent" : SUBJECT_KINDS[kind].label.toLowerCase(), kind === "all" ? "workflows and agents" : SUBJECT_KINDS[kind].plural.toLowerCase())}`,
        kind: kind === "all" ? null : kind,
        total: true,
        selected: focus === null,
        slot: undefined,
        weeks: scopeWeeks,
        peak: Math.max(1, ...scopeWeeks),
        waiting: scopeWaiting.count,
        overdue: scopeWaiting.overdue,
      }),
      ...rows,
    ]
  }, [overview, kind, focus, slots, weeks, now])

  const options = Object.fromEntries(
    (["all", ...kinds] as KindFilter[]).map((key) => [key, KIND_FILTERS[key]])
  ) as Record<KindFilter, string>

  return (
    <Ledger
      rows={rows}
      period={overview.period}
      onSelect={onSelect}
      toolbar={
        kinds.length > 1 && (
          <SplitSwitch
            label="Show"
            options={options}
            value={kind}
            onValueChange={onKind}
          />
        )
      }
    />
  )
}

/** A link to the focused workflow's or agent's builder, when it's still there. */
function OpenSubject({
  organizationId,
  subject,
}: {
  organizationId: string
  subject: { kind: SubjectKind; id: string; current: boolean }
}) {
  if (!subject.current || subject.kind === "assistant") return null
  const link =
    subject.kind === "workflow" ? (
      <Link
        to="/organizations/$organizationId/agents/$agentId"
        params={{ organizationId, agentId: subject.id }}
      />
    ) : (
      <Link
        to="/organizations/$organizationId/chat-agents/$chatAgentId"
        params={{ organizationId, chatAgentId: subject.id }}
      />
    )
  return (
    <Button
      variant="ghost"
      size="xs"
      className="text-muted-foreground"
      nativeButton={false}
      render={link}
    >
      <Icon icon="external" data-icon="inline-start" />
      Open {SUBJECT_KINDS[subject.kind].label.toLowerCase()}
    </Button>
  )
}
