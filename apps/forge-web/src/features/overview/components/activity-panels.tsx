import type * as React from "react"

import { Icon } from "@/components/forge/icon"
import type { ChartConfig } from "@/components/ui/chart"
import { SERIES_SWATCH, swatch } from "@/features/overview/components/chart-kit"
import {
  BarList,
  Delta,
  Facts,
  HandoffKey,
  Legend,
  Panel,
  type BarListRow,
} from "@/features/overview/components/panels"
import { UsageChart } from "@/features/overview/components/usage-panels"
import {
  age,
  compact,
  count,
  duration,
  percent,
  plural,
  rate,
  usd,
} from "@/features/overview/lib/format"
import {
  inScope,
  SUBJECT_KINDS,
  type OrganizationOverview,
} from "@/features/overview/lib/overview"
import {
  KIND_COLOR,
  KIND_ORDER,
  type UsageContext,
} from "@/features/overview/lib/series"
import { parseTimestamp } from "@/lib/timestamps"

/*
 * What the organization's workflows, agents and assistant did: how often
 * they ran and how it ended, who ran them, what in them used the tokens,
 * and what needs a person now.
 */

/* Activity ---------------------------------------------------------------- */

/** Workflow runs and agent invocations, stacked by type. */
export function ActivityPanel({ context }: { context: UsageContext }) {
  const { summary, unit, ticks } = context
  const { current, previous } = summary
  const kinds = KIND_ORDER.filter((kind) =>
    summary.buckets.some((bucket) => bucket.byKind[kind].started > 0)
  )
  const config: ChartConfig = Object.fromEntries(
    kinds.map((kind) => [
      kind,
      {
        label: `${SUBJECT_KINDS[kind].label} ${SUBJECT_KINDS[kind].runs}`,
        color: KIND_COLOR[kind].color,
      },
    ])
  )
  const rows = summary.buckets.map((bucket) => ({
    start: bucket.start,
    ...Object.fromEntries(
      kinds.map((kind) => [kind, bucket.byKind[kind].started])
    ),
  }))
  const totals = Object.fromEntries(
    kinds.map((kind) => [
      kind,
      summary.buckets.reduce(
        (sum, bucket) => sum + bucket.byKind[kind].started,
        0
      ),
    ])
  )
  const busiest = summary.buckets.reduce(
    (best, bucket) => (bucket.started > (best?.started ?? 0) ? bucket : best),
    undefined as (typeof summary.buckets)[number] | undefined
  )
  return (
    <Panel
      title="Runs and invocations"
      answer={
        current.started === 0 ? (
          "Nothing ran in this period."
        ) : (
          <>
            {count(current.started)} in all,{" "}
            {rate(current.started / Math.max(1, summary.buckets.length))} a{" "}
            {unit}.{" "}
            {previous.started > 0 && (
              <>
                {current.started >= previous.started ? "Up" : "Down"}{" "}
                {percent(Math.abs(current.started / previous.started - 1))} on
                the period before.
              </>
            )}
          </>
        )
      }
    >
      <UsageChart
        config={config}
        keys={kinds}
        rows={rows}
        unit={unit}
        ticks={ticks}
        format={count}
        axis={compact}
        caption="Runs and invocations by type"
        stackId="activity"
      />
      <Legend
        items={kinds.map((kind) => ({
          key: kind,
          label: `${SUBJECT_KINDS[kind].label} ${SUBJECT_KINDS[kind].runs}`,
          value: count(totals[kind] ?? 0),
          mark: swatch(KIND_COLOR[kind].swatch),
        }))}
      />
      <Facts
        items={[
          [
            "Busiest",
            busiest && busiest.started > 0
              ? `${new Date(busiest.start).toLocaleDateString(undefined, {
                  weekday: unit === "day" ? "short" : undefined,
                  month: "short",
                  day: "numeric",
                })} · ${count(busiest.started)}`
              : "None",
          ],
          ["Retried after a failure", count(current.retried)],
        ]}
      />
    </Panel>
  )
}

/* Outcomes ---------------------------------------------------------------- */

const outcomesConfig = {
  succeeded: { label: "Succeeded", color: "var(--chart-positive)" },
  other: { label: "Open or abandoned", color: "var(--chart-neutral)" },
  failed: { label: "Failed", color: "var(--chart-negative)" },
} satisfies ChartConfig

const OUTCOME_SWATCH = {
  succeeded: "bg-chart-positive",
  other: "bg-chart-neutral",
  failed: "bg-chart-negative",
} as const

/** How what started ended so far, on the Outcome Trio with a counted legend. */
export function OutcomesPanel({ context }: { context: UsageContext }) {
  const { summary, unit, ticks } = context
  const { current, successRate, previousSuccessRate, averageDurationMs } =
    summary
  const rows = summary.buckets.map((bucket) => ({
    start: bucket.start,
    succeeded: bucket.succeeded,
    other: bucket.open + bucket.abandoned,
    failed: bucket.failed,
  }))
  const other = current.open + current.abandoned
  return (
    <Panel
      title="Outcomes"
      answer={
        successRate === null ? (
          "Nothing finished in this period."
        ) : (
          <>
            {percent(successRate)} of what finished succeeded
            {previousSuccessRate !== null && (
              <span className="whitespace-nowrap">
                {" "}
                (
                <Delta
                  current={successRate}
                  previous={previousSuccessRate}
                  better="higher"
                  threshold={0.02}
                  format={(change) =>
                    `${Math.round(Math.abs(change) * 100)} pts`
                  }
                />
                )
              </span>
            )}
            {current.failed > 0 && <>; {plural(current.failed, "failure")}</>}.
          </>
        )
      }
    >
      <UsageChart
        config={outcomesConfig}
        keys={["succeeded", "other", "failed"]}
        rows={rows}
        unit={unit}
        ticks={ticks}
        format={count}
        axis={compact}
        caption="Outcomes by day"
        stackId="outcomes"
      />
      <Legend
        items={[
          {
            key: "succeeded",
            label: "Succeeded",
            value: count(current.succeeded),
            mark: swatch(OUTCOME_SWATCH.succeeded),
          },
          {
            key: "other",
            label: "Open or abandoned",
            value: count(other),
            mark: swatch(OUTCOME_SWATCH.other),
          },
          {
            key: "failed",
            label: "Failed",
            value: count(current.failed),
            mark: swatch(OUTCOME_SWATCH.failed),
          },
        ]}
      />
      <Facts
        items={[
          [
            "Average time to finish",
            averageDurationMs === null ? "None" : duration(averageDurationMs),
          ],
          ["Abandoned or cut off", count(current.abandoned)],
          ["Still open", count(current.open)],
        ]}
      />
    </Panel>
  )
}

/* People ------------------------------------------------------------------ */

const PEOPLE_ROWS = 6

/** Who ran the most, and what it used. */
export function PeoplePanel({
  overview,
  context,
}: {
  overview: OrganizationOverview
  context: UsageContext
}) {
  const { scope } = context
  const names = new Map(
    overview.members.map((member) => [member.id, member.name])
  )
  const people = new Map<
    string,
    { runs: number; tokens: number; cost: number }
  >()
  for (const fact of overview.people) {
    if (!inScope(scope, fact.subject) || !fact.user) continue
    const found = people.get(fact.user) ?? { runs: 0, tokens: 0, cost: 0 }
    found.runs += fact.runs
    found.tokens += fact.tokens
    found.cost += fact.cost
    people.set(fact.user, found)
  }
  const ranked = [...people.entries()].sort(
    (a, b) => b[1].tokens - a[1].tokens || b[1].runs - a[1].runs
  )
  const top = ranked[0]
  const total = ranked.reduce((sum, [, person]) => sum + person.tokens, 0)
  const rows: BarListRow[] = ranked
    .slice(0, PEOPLE_ROWS)
    .map(([user, person]) => ({
      key: user,
      label: names.get(user) ?? user,
      value: person.tokens,
      detail: plural(person.runs, "run"),
    }))
  return (
    <Panel
      title="People"
      answer={
        ranked.length === 0
          ? "Nobody ran anything in this period."
          : `${plural(ranked.length, "person", "people")} ran something${
              top && total > 0
                ? `; ${names.get(top[0]) ?? top[0]} used ${percent(top[1].tokens / total)} of the tokens.`
                : "."
            }`
      }
    >
      {rows.length > 0 && (
        <BarList rows={rows} format={(value) => `${compact(value)} tokens`} />
      )}
      {ranked.length > PEOPLE_ROWS && (
        <p className="mt-2 text-2xs text-muted-foreground">
          And {plural(ranked.length - PEOPLE_ROWS, "other")}.
        </p>
      )}
    </Panel>
  )
}

/* Steps and tools --------------------------------------------------------- */

/** What inside the workflows and agents used the tokens, and the tools they called. */
export function StepsPanel({
  overview,
  context,
}: {
  overview: OrganizationOverview
  context: UsageContext
}) {
  const { scope, slots, subjects } = context
  const steps = overview.steps
    .filter((step) => inScope(scope, step.subject))
    .sort((a, b) => b.tokens - a.tokens)
  const tools = new Map<string, { calls: number; failed: number }>()
  for (const fact of overview.tools) {
    if (!inScope(scope, fact.subject)) continue
    const found = tools.get(fact.tool) ?? { calls: 0, failed: 0 }
    found.calls += fact.calls
    found.failed += fact.failed
    tools.set(fact.tool, found)
  }
  const toolRows = [...tools.entries()].sort((a, b) => b[1].calls - a[1].calls)
  const toolCalls = toolRows.reduce((sum, [, tool]) => sum + tool.calls, 0)
  const showSubject = scope.level !== "subject"
  return (
    <Panel
      title="Steps and tools"
      answer={
        steps.length === 0 && toolCalls === 0
          ? "No step called a model or a tool in this period."
          : `${plural(steps.length, "step")} called models${
              toolCalls
                ? `; ${plural(toolCalls, "tool call")} to ${plural(toolRows.length, "tool")}`
                : ""
            }.`
      }
    >
      {steps.length > 0 && (
        <BarList
          rows={steps.slice(0, 5).map((step) => {
            const slot = slots.get(step.subject) ?? "other"
            return {
              key: `${step.subject}/${step.author}`,
              label: showSubject
                ? `${step.author || "Agent"} · ${subjects.get(step.subject)?.name ?? step.subject}`
                : step.author || "Agent",
              value: step.tokens,
              detail: usd(step.cost),
              mark: showSubject ? swatch(SERIES_SWATCH[slot]) : undefined,
            }
          })}
          format={compact}
          labelWidth="minmax(0,11rem)"
        />
      )}
      {toolRows.length > 0 && (
        <>
          <h4 className="mt-5 mb-2 text-xs font-medium">Tools</h4>
          <Facts
            className="mt-0"
            items={toolRows.slice(0, 5).map(([tool, found]) => [
              <span key={tool} className="font-mono text-2xs">
                {tool}
              </span>,
              <>
                {count(found.calls)}
                {found.failed > 0 && (
                  <span className="ml-1.5 font-normal text-danger-foreground">
                    {count(found.failed)} failed
                  </span>
                )}
              </>,
            ])}
          />
        </>
      )}
    </Panel>
  )
}

/* Attention --------------------------------------------------------------- */

/** Runs waiting on a person now, oldest first, and why runs failed. */
export function AttentionPanel({
  overview,
  context,
  now,
  onOpenRun,
}: {
  overview: OrganizationOverview
  context: UsageContext
  now: number
  /** Open a waiting run's page; without it, runs are listed only. */
  onOpenRun?: (runId: string) => void
}) {
  const { scope, subjects } = context
  const waiting = overview.waiting.filter((run) => inScope(scope, run.subject))
  const failures = overview.failures.filter((failure) =>
    inScope(scope, failure.subject)
  )
  const oldest = waiting[0]
  const waited = (since: string | null) =>
    since ? Math.max(0, now - parseTimestamp(since).getTime()) : 0
  return (
    <Panel
      title="Needs attention"
      answer={
        waiting.length === 0 && failures.length === 0
          ? "Nothing waits on people, and nothing failed."
          : waiting.length
            ? `${plural(waiting.length, "run")} ${waiting.length === 1 ? "waits" : "wait"} on people, the oldest for ${age(waited(oldest?.since ?? null))}.`
            : "Nothing waits on people."
      }
    >
      {waiting.length > 0 && (
        <ul className="border-t text-xs">
          {waiting.slice(0, 5).map((run) => (
            <li key={run.run_id} className="border-b last:border-b-0">
              <WaitingRow onOpen={onOpenRun && (() => onOpenRun(run.run_id))}>
                <HandoffKey
                  label={run.run_id.slice(0, 6)}
                  overdue={waited(run.since) >= 86_400_000}
                />
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-foreground">
                    {run.reason ||
                      (run.kind === "approval" ? "Approval" : "Question")}
                  </span>
                  <span className="block truncate text-2xs text-muted-foreground">
                    {subjects.get(run.subject)?.name ?? run.subject} · waiting{" "}
                    {age(waited(run.since))}
                  </span>
                </span>
                {onOpenRun && (
                  <Icon
                    icon="right"
                    size={14}
                    className="shrink-0 text-subtle"
                    aria-hidden="true"
                  />
                )}
              </WaitingRow>
            </li>
          ))}
        </ul>
      )}
      {failures.length > 0 && (
        <>
          <h4 className="mt-5 mb-2 text-xs font-medium">Why runs failed</h4>
          <Facts
            className="mt-0"
            items={failures.slice(0, 5).map((failure, index) => [
              <span
                key={index}
                className="block max-w-[16rem] truncate"
                title={failure.message}
              >
                {subjects.get(failure.subject)?.name ?? failure.subject}
                {failure.step && ` · ${failure.step}`}: {failure.message}
              </span>,
              count(failure.count),
            ])}
          />
        </>
      )}
    </Panel>
  )
}

/** A waiting run's row: a button to its page, or plain when it can't be opened. */
function WaitingRow({
  onOpen,
  children,
}: {
  onOpen?: () => void
  children: React.ReactNode
}) {
  const className = "flex w-full items-center gap-2.5 py-2 text-left"
  if (!onOpen) return <div className={className}>{children}</div>
  return (
    <button
      type="button"
      onClick={onOpen}
      className={`${className} hover:text-foreground`}
    >
      {children}
    </button>
  )
}
