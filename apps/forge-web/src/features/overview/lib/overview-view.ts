import {
  DEFAULT_PERIOD,
  isOverviewPeriod,
  type OverviewPeriod,
  type OverviewSpan,
  type SubjectKind,
} from "@/features/overview/lib/overview"

/*
 * What an organization's overview shows, as its page's address keeps it, so
 * it can be linked to and outlasts a reload: its period (`period`, or the
 * days picked, `from` and `to`), and its focus (`only`, the ledger's type,
 * and `focus`, the workflow or agent picked in the ledger or the header's
 * picker). Every widget follows the focus. Defaults are left out.
 */

export type OverviewView = {
  /** 7 days when absent and no days are picked. */
  period?: OverviewPeriod
  /** Days picked instead, first to last (YYYY-MM-DD). */
  from?: string
  to?: string
  /** Workflows, agents or the assistant only; everything when absent. */
  only?: SubjectKind
  /** A workflow's or agent's key (`agent:<id>`); the whole when absent. */
  focus?: string
}

const DAY = /^\d{4}-\d{2}-\d{2}$/
const SUBJECT = /^(workflow|agent|assistant):[^\s]{1,255}$/
const KINDS: readonly SubjectKind[] = ["workflow", "agent", "assistant"]

const isDay = (value: unknown): value is string =>
  typeof value === "string" && DAY.test(value)

/** The overview's part of a page's search, kept to what it can show. */
export function overviewViewOf(search: Record<string, unknown>): OverviewView {
  const days =
    isDay(search.from) && isDay(search.to) && search.from <= search.to
      ? { from: search.from, to: search.to }
      : undefined
  return {
    ...(days ??
      (isOverviewPeriod(search.period) &&
        search.period !== DEFAULT_PERIOD && { period: search.period })),
    ...((KINDS as readonly unknown[]).includes(search.only) && {
      only: search.only as SubjectKind,
    }),
    ...(typeof search.focus === "string" &&
      SUBJECT.test(search.focus) && { focus: search.focus }),
  }
}

/** What the overview reads: the days picked, or its period. */
export const spanOf = (view: OverviewView): OverviewSpan =>
  view.from && view.to
    ? { from: view.from, to: view.to }
    : (view.period ?? DEFAULT_PERIOD)

/**
 * A search with the overview's part changed: picking days leaves the
 * period aside and a period the days; defaults go.
 */
export function withView<S extends Record<string, unknown>>(
  search: S,
  change: Partial<OverviewView>
): S {
  const next: Record<string, unknown> = { ...search, ...change }
  if ("from" in change || "to" in change) next.period = undefined
  if ("period" in change) {
    next.from = undefined
    next.to = undefined
  }
  if (next.period === DEFAULT_PERIOD) next.period = undefined
  return next as S
}
