import * as React from "react"
import { keepPreviousData, useQuery } from "@tanstack/react-query"

import { api } from "@/lib/api-instance"
import { parseTimestamp } from "@/lib/timestamps"

/*
 * An organization's overview: what its workflows, agents and assistant did
 * over the last 7, 30 or 90 days, and what it cost. The admin API reads it
 * (`GET /organizations/{org}/overview?period=&time_zone=`) from three
 * records: its workflow runs (the run store), every ADK invocation its
 * workflows, agents and assistant made, and every model and tool call in
 * them, with the tokens each model call reported and its price.
 *
 * The API buckets the period by day (by week for 90 days) in the browser's
 * time zone and sums each bucket by workflow or agent (and by model); the
 * page sums those for whatever it shows: the whole organization, one kind,
 * or one workflow or agent picked in the ledger. Bucket -1 is the period
 * before, which every change compares with.
 */

/* Contract ---------------------------------------------------------------- */

export type OverviewPeriod = "7d" | "30d" | "90d"

/** The periods the overview offers; charts bucket by day, or by week for 90. */
export const OVERVIEW_PERIODS: Record<
  OverviewPeriod,
  { label: string; days: number; bucket: "day" | "week" }
> = {
  "7d": { label: "7 days", days: 7, bucket: "day" },
  "30d": { label: "30 days", days: 30, bucket: "day" },
  "90d": { label: "90 days", days: 90, bucket: "week" },
}

export const DEFAULT_PERIOD: OverviewPeriod = "7d"

export const isOverviewPeriod = (value: unknown): value is OverviewPeriod =>
  typeof value === "string" && Object.hasOwn(OVERVIEW_PERIODS, value)

/**
 * What did the work: a workflow (its runs), an agent (its invocations, from
 * the API, a test or a workflow) or the organization's assistant.
 */
export type SubjectKind = "workflow" | "agent" | "assistant"

export const SUBJECT_KINDS: Record<
  SubjectKind,
  { label: string; plural: string; run: string; runs: string }
> = {
  workflow: {
    label: "Workflow",
    plural: "Workflows",
    run: "run",
    runs: "runs",
  },
  agent: {
    label: "Agent",
    plural: "Agents",
    run: "invocation",
    runs: "invocations",
  },
  assistant: {
    label: "Assistant",
    plural: "Assistant",
    run: "conversation turn",
    runs: "conversation turns",
  },
}

export type OverviewSubject = {
  /** `<kind>:<id>`: what the facts name it by. */
  key: string
  kind: SubjectKind
  id: string
  name: string
  /** It's still there: deleted ones keep their history, without a page. */
  current: boolean
  /** Its latest run, invocation or model call, whenever it was. */
  last_at: string | null
}

export type OverviewBucket = { start: string; end: string }

/** Model calls in a bucket, of one workflow or agent, to one model. */
export type UsageFact = {
  /** The bucket's index; -1 for the period before. */
  bucket: number
  subject: string
  model: string
  calls: number
  /** Calls the model answered with an error. */
  failed: number
  /** Prompt tokens, cached ones included. */
  input: number
  /** Of the input, read back from the provider's cache. */
  cached: number
  /** Response tokens, thinking ones included. */
  output: number
  /** Of the output, the model's reasoning. */
  thinking: number
  /** USD, for the calls with a known price. */
  cost: number
  /** Calls whose model had no known price. */
  unpriced: number
}

/**
 * Runs (a workflow's, from the run store) or invocations (an agent's or the
 * assistant's) that started in a bucket, by how they've ended so far.
 */
export type ActivityFact = {
  bucket: number
  subject: string
  started: number
  succeeded: number
  failed: number
  abandoned: number
  /** Not finished: queued, running, or waiting for a person or a time. */
  open: number
  /** Workflow runs taken again after a failure, a hiccup or an interruption. */
  retried: number
  /** Over the finished ones with a start: their time from start to finish. */
  duration_ms: number
  timed: number
}

/** A workflow's or agent's week, over the last 12 weeks, for the ledger's bars. */
export type WeekFact = {
  subject: string
  /** 0 is 11 weeks ago; 11 is this week so far. */
  week: number
  tokens: number
  runs: number
}

/** Over the period: a step or sub-agent of a workflow or agent, by what it called. */
export type StepFact = {
  subject: string
  /** The ADK agent (a workflow's step, an agent or a sub-agent) that called. */
  author: string
  calls: number
  tokens: number
  cost: number
}

/** Over the period: tools called (MCP, HTTP, functions), by workflow or agent. */
export type ToolFact = {
  subject: string
  tool: string
  calls: number
  failed: number
}

/** Over the period: what a member ran and used, by workflow or agent. */
export type PersonFact = {
  subject: string
  /** A member's ID; for hosted runs, the caller's user ID as it passed it. */
  user: string
  runs: number
  calls: number
  tokens: number
  cost: number
}

/** A workflow run waiting on a person now: an approval or a question. */
export type WaitingRun = {
  run_id: string
  subject: string
  kind: "approval" | "human_input"
  reason: string
  since: string
}

/** Over the period: why runs failed, by workflow and step. */
export type FailureFact = {
  subject: string
  step: string | null
  message: string
  count: number
  last_at: string
}

export type OrganizationOverview = {
  period: OverviewPeriod
  unit: "day" | "week"
  time_zone: string
  /** When the API read it. */
  as_of: string
  /** The period's buckets, oldest first; the last runs to `as_of`. */
  buckets: OverviewBucket[]
  /** The period before, which changes compare with. */
  previous: OverviewBucket
  /** The last 12 weeks, Monday to Monday, for the ledger's bars. */
  weeks: OverviewBucket[]
  subjects: OverviewSubject[]
  /** Names of the users the facts name, where they're known. */
  members: { id: string; name: string }[]
  usage: UsageFact[]
  activity: ActivityFact[]
  weekly: WeekFact[]
  steps: StepFact[]
  tools: ToolFact[]
  people: PersonFact[]
  waiting: WaitingRun[]
  failures: FailureFact[]
  /** Workflow runs not finished now, by where they are. */
  open: { queued: number; running: number; paused: number; waiting: number }
  /** The first model call recorded, ever; none before usage was recorded. */
  recording_since: string | null
  /** More workflow runs than the API reads at once: the oldest are left out. */
  truncated: boolean
}

/* Reading ----------------------------------------------------------------- */

/** The browser's time zone, which the API buckets days in. */
const timeZone = () => {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC"
  } catch {
    return "UTC"
  }
}

export const overviewKeys = {
  all: ["organization-overview"] as const,
  of: (organizationId: string, period: OverviewPeriod, zone: string) =>
    [...overviewKeys.all, organizationId, period, zone] as const,
}

/** Every couple of minutes is live enough for a daily overview. */
const REFRESH_MS = 120_000

/** The organization's overview for a period; the last one stays while the next loads. */
export function useOrganizationOverview(
  organizationId: string,
  period: OverviewPeriod
) {
  const [zone] = React.useState(timeZone)
  return useQuery({
    queryKey: overviewKeys.of(organizationId, period, zone),
    queryFn: ({ signal }) =>
      api.get<OrganizationOverview>(
        `/organizations/${encodeURIComponent(organizationId)}/overview`,
        { params: { period, time_zone: zone }, signal }
      ),
    placeholderData: keepPreviousData,
    refetchInterval: REFRESH_MS,
  })
}

/* Summing ----------------------------------------------------------------- */

/** What the overview shows: the organization, one kind, or one workflow or agent. */
export type OverviewScope =
  | { level: "organization" }
  | { level: "kind"; kind: SubjectKind }
  | { level: "subject"; key: string }

export type Tokens = {
  input: number
  cached: number
  output: number
  thinking: number
}

export type Totals = Tokens & {
  /** Input plus output (cached and thinking are part of them). */
  tokens: number
  calls: number
  failedCalls: number
  cost: number
  unpriced: number
  started: number
  succeeded: number
  failed: number
  abandoned: number
  open: number
  retried: number
  durationMs: number
  timed: number
}

export type BucketTotals = Totals & {
  start: number
  end: number
  /** Tokens, cost and runs by kind, by subject and by model. */
  byKind: Record<SubjectKind, { tokens: number; cost: number; started: number }>
  bySubject: Record<string, { tokens: number; cost: number; started: number }>
  byModel: Record<string, { tokens: number; cost: number; calls: number }>
}

export type ModelTotals = Tokens & {
  model: string
  tokens: number
  calls: number
  failed: number
  cost: number
  unpriced: number
}

export type OverviewSummary = {
  current: Totals
  previous: Totals
  buckets: BucketTotals[]
  models: ModelTotals[]
  /** Finished runs that succeeded; null when none finished. */
  successRate: number | null
  previousSuccessRate: number | null
  /** Mean time from start to finish; null when none finished. */
  averageDurationMs: number | null
}

/** Totals of nothing. */
export const emptyTotals = (): Totals => ({
  input: 0,
  cached: 0,
  output: 0,
  thinking: 0,
  tokens: 0,
  calls: 0,
  failedCalls: 0,
  cost: 0,
  unpriced: 0,
  started: 0,
  succeeded: 0,
  failed: 0,
  abandoned: 0,
  open: 0,
  retried: 0,
  durationMs: 0,
  timed: 0,
})

const KINDS: SubjectKind[] = ["workflow", "agent", "assistant"]

const addUsage = (totals: Totals, fact: UsageFact) => {
  totals.input += fact.input
  totals.cached += fact.cached
  totals.output += fact.output
  totals.thinking += fact.thinking
  totals.tokens += fact.input + fact.output
  totals.calls += fact.calls
  totals.failedCalls += fact.failed
  totals.cost += fact.cost
  totals.unpriced += fact.unpriced
}

const addActivity = (totals: Totals, fact: ActivityFact) => {
  totals.started += fact.started
  totals.succeeded += fact.succeeded
  totals.failed += fact.failed
  totals.abandoned += fact.abandoned
  totals.open += fact.open
  totals.retried += fact.retried
  totals.durationMs += fact.duration_ms
  totals.timed += fact.timed
}

export const kindOf = (subjectKey: string) =>
  subjectKey.slice(0, subjectKey.indexOf(":")) as SubjectKind

/** Whether a workflow or agent is in the scope. */
export const inScope = (scope: OverviewScope, subjectKey: string) =>
  scope.level === "organization" ||
  (scope.level === "kind"
    ? kindOf(subjectKey) === scope.kind
    : subjectKey === scope.key)

export const successRateOf = ({ succeeded, failed, abandoned }: Totals) => {
  const finished = succeeded + failed + abandoned
  return finished ? succeeded / finished : null
}

/** The scope's figures over the period and the one before, bucket by bucket. */
export function summarize(
  overview: OrganizationOverview,
  scope: OverviewScope
): OverviewSummary {
  const current = emptyTotals()
  const previous = emptyTotals()
  const buckets: BucketTotals[] = overview.buckets.map((bucket) => ({
    ...emptyTotals(),
    start: parseTimestamp(bucket.start).getTime(),
    end: parseTimestamp(bucket.end).getTime(),
    byKind: Object.fromEntries(
      KINDS.map((kind) => [kind, { tokens: 0, cost: 0, started: 0 }])
    ) as BucketTotals["byKind"],
    bySubject: {},
    byModel: {},
  }))
  const models = new Map<string, ModelTotals>()

  for (const fact of overview.usage) {
    if (!inScope(scope, fact.subject)) continue
    if (fact.bucket < 0) {
      addUsage(previous, fact)
      continue
    }
    addUsage(current, fact)
    const tokens = fact.input + fact.output
    const bucket = buckets[fact.bucket]
    if (bucket) {
      addUsage(bucket, fact)
      const kind = bucket.byKind[kindOf(fact.subject)]
      if (kind) {
        kind.tokens += tokens
        kind.cost += fact.cost
      }
      const subject = (bucket.bySubject[fact.subject] ??= {
        tokens: 0,
        cost: 0,
        started: 0,
      })
      subject.tokens += tokens
      subject.cost += fact.cost
      const model = (bucket.byModel[fact.model] ??= {
        tokens: 0,
        cost: 0,
        calls: 0,
      })
      model.tokens += tokens
      model.cost += fact.cost
      model.calls += fact.calls
    }
    const model = models.get(fact.model) ?? {
      model: fact.model,
      input: 0,
      cached: 0,
      output: 0,
      thinking: 0,
      tokens: 0,
      calls: 0,
      failed: 0,
      cost: 0,
      unpriced: 0,
    }
    model.input += fact.input
    model.cached += fact.cached
    model.output += fact.output
    model.thinking += fact.thinking
    model.tokens += tokens
    model.calls += fact.calls
    model.failed += fact.failed
    model.cost += fact.cost
    model.unpriced += fact.unpriced
    models.set(fact.model, model)
  }

  for (const fact of overview.activity) {
    if (!inScope(scope, fact.subject)) continue
    if (fact.bucket < 0) {
      addActivity(previous, fact)
      continue
    }
    addActivity(current, fact)
    const bucket = buckets[fact.bucket]
    if (bucket) {
      addActivity(bucket, fact)
      const kind = bucket.byKind[kindOf(fact.subject)]
      if (kind) kind.started += fact.started
      const subject = (bucket.bySubject[fact.subject] ??= {
        tokens: 0,
        cost: 0,
        started: 0,
      })
      subject.started += fact.started
    }
  }

  return {
    current,
    previous,
    buckets,
    models: [...models.values()].sort(
      (a, b) => b.tokens - a.tokens || a.model.localeCompare(b.model)
    ),
    successRate: successRateOf(current),
    previousSuccessRate: successRateOf(previous),
    averageDurationMs: current.timed
      ? current.durationMs / current.timed
      : null,
  }
}

/** Each workflow's or agent's figures over the period, by its key. */
export function bySubject(overview: OrganizationOverview) {
  const totals = new Map<string, { current: Totals; previous: Totals }>()
  const of = (key: string) => {
    let entry = totals.get(key)
    if (!entry) {
      entry = { current: emptyTotals(), previous: emptyTotals() }
      totals.set(key, entry)
    }
    return entry
  }
  for (const fact of overview.usage) {
    const entry = of(fact.subject)
    addUsage(fact.bucket < 0 ? entry.previous : entry.current, fact)
  }
  for (const fact of overview.activity) {
    const entry = of(fact.subject)
    addActivity(fact.bucket < 0 ? entry.previous : entry.current, fact)
  }
  return totals
}

/** Each workflow's or agent's last 12 weeks of tokens (or runs), by its key. */
export function weeksBySubject(
  overview: OrganizationOverview,
  measure: "tokens" | "runs"
) {
  const weeks = new Map<string, number[]>()
  const length = overview.weeks.length
  for (const fact of overview.weekly) {
    const row = weeks.get(fact.subject) ?? Array<number>(length).fill(0)
    if (fact.week >= 0 && fact.week < length) row[fact.week] += fact[measure]
    weeks.set(fact.subject, row)
  }
  return weeks
}
