import { since } from "@/features/overview/lib/format"
import {
  SUBJECT_KINDS,
  successRateOf,
  type SubjectKind,
  type Totals,
} from "@/features/overview/lib/overview"
import type { SeriesSlot } from "@/features/overview/components/chart-kit"

/*
 * The overview ledger's rows: every workflow and agent (and the assistant)
 * compared on the same figures, and the whole scope's above them.
 */

/** The ledger's row for the whole scope, pinned above the rest. */
export const SCOPE_ROW = "scope"

export type LedgerRow = {
  /** `SCOPE_ROW`, or the workflow's or agent's key. */
  id: string
  name: string
  detail: string
  kind: SubjectKind | null
  total: boolean
  selected: boolean
  slot: SeriesSlot | undefined
  /** Tokens each of the last 12 weeks, and the top of their bars' scale. */
  weeks: number[]
  peak: number
  runs: number
  runsBefore: number
  success: number | null
  successBefore: number | null
  tokens: number
  tokensBefore: number
  calls: number
  cost: number
  costPerRun: number | null
  averageMs: number | null
  waiting: number
  overdue: number
}

export function ledgerRow(
  current: Totals,
  previous: Totals,
  fields: Pick<
    LedgerRow,
    | "id"
    | "name"
    | "detail"
    | "kind"
    | "total"
    | "selected"
    | "slot"
    | "weeks"
    | "peak"
    | "waiting"
    | "overdue"
  >
): LedgerRow {
  return {
    ...fields,
    runs: current.started,
    runsBefore: previous.started,
    success: successRateOf(current),
    successBefore: successRateOf(previous),
    tokens: current.tokens,
    tokensBefore: previous.tokens,
    calls: current.calls,
    cost: current.cost,
    costPerRun: current.started ? current.cost / current.started : null,
    averageMs: current.timed ? current.durationMs / current.timed : null,
  }
}

/** A workflow's or agent's second line: its type and when it last ran. */
export function detailOf(
  kind: SubjectKind,
  current: boolean,
  lastAt: string | null,
  now: number
) {
  const label = SUBJECT_KINDS[kind].label
  if (!current && kind !== "assistant") return `${label} · deleted`
  return `${label} · ${lastAt ? `active ${since(lastAt, now)}` : "not run yet"}`
}
