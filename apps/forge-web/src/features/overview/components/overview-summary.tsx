import * as React from "react"

import type { SeriesSlot } from "@/features/overview/components/chart-kit"
import { Ledger } from "@/features/overview/components/ledger"
import {
  Fact,
  ReadOutText,
  SplitSwitch,
} from "@/features/overview/components/panels"
import { WidgetTitle } from "@/features/overview/components/widget-title"
import {
  ageInWords,
  compact,
  DAY,
  plural,
  usd,
} from "@/features/overview/lib/format"
import {
  detailOf,
  ledgerRow,
  SCOPE_ROW,
  type LedgerRow,
} from "@/features/overview/lib/ledger"
import {
  bySubject,
  emptyTotals,
  periodWords,
  SUBJECT_KINDS,
  summarize,
  type OrganizationOverview as Overview,
  type SubjectKind,
} from "@/features/overview/lib/overview"
import { parseTimestamp } from "@/lib/timestamps"

/*
 * The overview's summary widgets: the read-out sentence on the period, and
 * the ledger of its workflows and agents, whose type switch and rows focus
 * every other widget on what's picked. They size to their text and rows.
 */

/** What the ledger shows: everything, or one type of thing. */
export type KindFilter = "all" | SubjectKind

/* Read-out ---------------------------------------------------------------- */

/**
 * The period in two sentences: what ran and what it used and cost; then
 * what waits on people. Always the whole organization, whatever's focused.
 */
export function ReadOut({
  overview,
  now,
}: {
  overview: Overview
  now: number
}) {
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
    // The grid spaces widgets; the sentence keeps no margin of its own.
    <ReadOutText className="mt-0 mb-0">
      {periodWords(overview).opening}{" "}
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

/** What the ledger shows and focuses, which every other widget follows. */
export type LedgerFocus = {
  kind: KindFilter
  /** The types the organization has something of. */
  kinds: SubjectKind[]
  /** The selected workflow's or agent's key; null for the whole. */
  focus: string | null
  /** Each one's tokens over the last 12 weeks. */
  weeks: Map<string, number[]>
  onKind: (kind: KindFilter) => void
  onSelect: (id: string) => void
}

const KIND_FILTERS: Record<KindFilter, string> = {
  all: "All",
  workflow: "Workflows",
  agent: "Agents",
  assistant: "Assistant",
}

export function OverviewLedger({
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
  const own = React.useContext(WidgetTitle)

  return (
    <Ledger
      rows={rows}
      within={periodWords(overview).opening}
      title={own}
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
