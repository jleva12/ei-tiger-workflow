import * as React from "react"

import {
  EmptyIllustration,
  EmptyWorkspace,
  PageEmpty,
} from "@/components/forge/empty-state"
import { ErrorCallout, LoadMore } from "@/components/forge/feedback"
import type { IconProp } from "@/components/forge/icons"
import { StatusBadge } from "@/components/forge/status"
import {
  DateLabel,
  TaskGroup,
  TaskList,
  TaskListSkeleton,
  TaskTableCell,
  TaskTableHead,
  TaskTableRow,
  TaskTitle,
} from "@/components/forge/task-list"
import type { TaskStatus } from "@/components/forge/variants"
import { Button } from "@/components/ui/button"
import {
  Table,
  TableBody,
  TableCell,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import {
  shortId,
  type AdkRun,
  type useOrganizationAdkRuns,
} from "@/lib/agents/runs"
import { formatDuration, formatShortDateTime } from "@/lib/format"
import { RUN_GROUPS, runNote, runStatusDisplay } from "@/lib/runs"

/** What the list says about what's run. */
export type RunListWords = {
  /** The list's title, e.g. "Runs". */
  title: string
  /** The list in a sentence, e.g. "runs". */
  list: string
  /** What runs, e.g. "ADK workflows". */
  docs: string
  /** One of them, e.g. "an ADK workflow". */
  aDoc: string
  icon: IconProp
}

/** A row of a group: one run of one of the organization's ADK workflows. */
type RunRow = {
  id: string
  /** The run's short ID, as its page's reference. */
  reference: string
  /** The ADK workflow's name, as it was when the run started. */
  title: string
  status: TaskStatus
  statusLabel: string
  note: string | undefined
  started: string | undefined
  duration: string | undefined
}

function toRow(run: AdkRun): RunRow {
  const shown = runStatusDisplay(run)
  return {
    id: run.id,
    reference: shortId(run.id),
    title: run.agent_name.trim() || run.agent_id,
    status: shown.status,
    statusLabel: shown.label,
    note: runNote(run),
    started: run.started_at ? formatShortDateTime(run.started_at) : undefined,
    duration:
      run.duration_ms === null ? undefined : formatDuration(run.duration_ms),
  }
}

/**
 * A status group's runs, in the Forge task list's rows and cells: the
 * run's reference and workflow, its status, what stands out about it
 * (what it waits for, why it failed), when it started and how long it took.
 * Scrolls sideways below 760px, like the task table.
 */
function RunTable({
  runs,
  onSelect,
}: {
  runs: RunRow[]
  onSelect: (run: RunRow) => void
}) {
  return (
    <Table
      data-slot="task-table"
      className="min-w-[760px] table-fixed border-separate border-spacing-0 @max-[800px]/shell:min-w-[800px]"
    >
      <colgroup>
        <col className="w-[40%] @max-[1270px]/shell:w-[36%]" />
        <col className="w-[15%]" />
        <col className="w-[22%] @max-[1270px]/shell:w-[24%]" />
        <col className="w-[14%] @max-[1270px]/shell:w-[15%]" />
        <col className="w-[9%] @max-[1270px]/shell:w-[10%]" />
      </colgroup>
      <TableHeader className="[&_tr]:border-0">
        <TableRow className="border-0 hover:bg-transparent">
          <TaskTableHead>Name</TaskTableHead>
          <TaskTableHead>Status</TaskTableHead>
          <TaskTableHead icon={null}>Details</TaskTableHead>
          <TaskTableHead icon="clock">Started</TaskTableHead>
          <TaskTableHead icon={null} className="pr-[17px] pl-0 text-right">
            Duration
          </TaskTableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {runs.map((run) => (
          <TaskTableRow key={run.id}>
            <TaskTableCell>
              <TaskTitle
                id={run.reference}
                title={run.title}
                onClick={() => onSelect(run)}
              />
            </TaskTableCell>
            <TaskTableCell>
              <StatusBadge status={run.status}>{run.statusLabel}</StatusBadge>
            </TaskTableCell>
            <TaskTableCell>
              {run.note ? (
                <span
                  className="block truncate text-2xs text-muted-foreground"
                  title={run.note}
                >
                  {run.note}
                </span>
              ) : (
                <span className="text-2xs text-subtle">—</span>
              )}
            </TaskTableCell>
            <TaskTableCell>
              <DateLabel>{run.started ?? "Not started"}</DateLabel>
            </TaskTableCell>
            <TaskTableCell className="pr-[17px] text-right text-2xs text-muted-foreground tabular-nums">
              {run.duration ?? "—"}
            </TaskTableCell>
          </TaskTableRow>
        ))}
        {!runs.length && (
          <TableRow className="border-0 hover:bg-transparent">
            <TableCell
              colSpan={5}
              className="h-[34px] px-[18px] pt-[5px] pb-[13px] text-2xs text-subtle"
            >
              No runs here
            </TableCell>
          </TableRow>
        )}
      </TableBody>
    </Table>
  )
}

/**
 * Every run of the organization's ADK workflows, in the Forge task list's
 * status bands (queued or waiting, running, waiting for a person,
 * succeeded, failed, abandoned), newest first in each. Each opens its own page, where
 * approvals are decided and failed runs retried. It follows runs in
 * progress every few seconds; the page's toolbar refreshes it on demand.
 */
export function RunList({
  organizationName,
  runs,
  words,
  onOpenRun,
  onShowOverview,
}: {
  organizationName: string
  /** The organization's runs (`useOrganizationAdkRuns`), shared with the tab's count. */
  runs: ReturnType<typeof useOrganizationAdkRuns>
  /** What's run. */
  words: RunListWords
  onOpenRun: (runId: string) => void
  onShowOverview: () => void
}) {
  const rows = React.useMemo(
    () => runs.data?.items.map(toRow),
    [runs.data?.items]
  )
  const groups = React.useMemo(
    () =>
      RUN_GROUPS.map((group) => ({
        ...group,
        runs: rows?.filter((row) => row.status === group.id) ?? [],
      })),
    [rows]
  )

  const retry = (
    <Button variant="outline" size="sm" onClick={() => void runs.refetch()}>
      Retry
    </Button>
  )

  if (runs.error && !runs.data) {
    // Their database isn't answering: the API says so.
    if (runs.error.status === 503) {
      return (
        <PageEmpty
          illustration="offline"
          title={`${words.title} are unavailable`}
          description={runs.error.message}
        >
          {retry}
        </PageEmpty>
      )
    }
    return (
      <ErrorCallout title={`Couldn't load the ${words.list}`} action={retry}>
        {runs.error.message}
      </ErrorCallout>
    )
  }

  if (!rows) return <TaskListSkeleton />

  if (rows.length === 0) {
    return (
      <EmptyWorkspace
        illustration={<EmptyIllustration name="waiting" />}
        title={`None of ${organizationName}'s ${words.docs} has run yet`}
        description={`Run ${words.aDoc} from its builder, and it shows here at once, by status, from queued to succeeded. Open a run to follow it, decide what it waits for, or retry it.`}
        actions={
          <Button variant="outline" onClick={onShowOverview}>
            Pick {words.aDoc}
          </Button>
        }
        steps={[
          { icon: words.icon, label: `Open ${words.aDoc}` },
          { icon: "sparkles", label: "Run it" },
          { icon: "activity", label: "Follow it here" },
        ]}
      />
    )
  }

  const loaded = rows.length
  const total = runs.data?.total ?? loaded

  return (
    <>
      <header className="mb-4 px-4">
        <h2 className="text-sm font-[550]">{words.title}</h2>
        <p className="mt-0.5 text-2xs text-muted-foreground">
          Every run of {organizationName}'s {words.docs}, by status and newest
          first. Open one to follow it, decide what it waits for, or retry it.
        </p>
      </header>
      <TaskList>
        {groups.map((group) => (
          <TaskGroup
            key={group.id}
            title={group.title}
            count={group.runs.length}
            symbol={group.symbol}
          >
            <RunTable runs={group.runs} onSelect={(run) => onOpenRun(run.id)} />
          </TaskGroup>
        ))}
      </TaskList>
      {runs.hasNextPage && (
        <LoadMore
          label={runs.isFetchingNextPage ? "Loading…" : "Load more runs"}
          hint={`Showing ${loaded.toLocaleString()} of ${total.toLocaleString()}`}
          disabled={runs.isFetchingNextPage}
          onClick={() => void runs.fetchNextPage()}
        />
      )}
    </>
  )
}
