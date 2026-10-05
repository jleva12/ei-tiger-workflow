import * as React from "react"
import { cn } from "cn"

import {
  createColumnHelper,
  DataTable,
  type DataTableFeatureConfig,
  type Row,
} from "@/components/forge/data-table"
import { Icon } from "@/components/forge/icon"
import { ADMIN_TABLE_FEATURES } from "@/features/admin/components/table-config"
import { SERIES_SWATCH } from "@/features/overview/components/chart-kit"
import {
  Delta,
  Figure,
  HandoffKey,
  VolumeDelta,
  WeekBars,
} from "@/features/overview/components/panels"
import { compact, duration, percent, usd } from "@/features/overview/lib/format"
import { SCOPE_ROW, type LedgerRow } from "@/features/overview/lib/ledger"
import {
  OVERVIEW_PERIODS,
  SUBJECT_KINDS,
  type OverviewPeriod,
} from "@/features/overview/lib/overview"

/*
 * The overview's ledger: every workflow and agent (and the assistant) the
 * organization has or used, compared on the same figures, with the whole
 * scope pinned on top. Selecting a row focuses everything below on it.
 */

// A handful to a few dozen rows: sort, size, hide and export them; the
// scope stays on top.
const LEDGER_FEATURES: Partial<DataTableFeatureConfig> = {
  ...ADMIN_TABLE_FEATURES,
  globalFilter: false,
  pagination: false,
  faceting: false,
  rowPinning: true,
}

const PIN_SCOPE = {
  state: { rowPinning: { top: [SCOPE_ROW], bottom: [] } },
  // Pinned by the page, not by people.
  enableRowPinning: false,
}

// The selected row fills Pale Mist. The pinned scope row otherwise looks like
// any other, so the fill only ever marks the selection.
const selectedRow = (row: Row<LedgerRow>) =>
  row.original.selected
    ? "bg-muted hover:bg-muted data-[pinned]:bg-muted"
    : "data-[pinned]:bg-background data-[pinned]:hover:bg-[color-mix(in_oklch,var(--muted)_60%,var(--background))]"

/** The row's mark: its colour slot, or graphite when it used nothing. */
function Mark({ row }: { row: LedgerRow }) {
  if (row.total) {
    return (
      <Icon
        icon="dashboard"
        size={15}
        className="shrink-0 text-muted-foreground"
      />
    )
  }
  return (
    <span
      aria-hidden="true"
      className={cn(
        "mx-[3.5px] size-2 shrink-0 rounded-[3px]",
        row.slot ? SERIES_SWATCH[row.slot] : "bg-chart-4"
      )}
    />
  )
}

function Waiting({ count, overdue }: { count: number; overdue: number }) {
  if (count === 0) return <span className="text-muted-foreground">None</span>
  return (
    <>
      <HandoffKey
        label={count.toLocaleString()}
        overdue={overdue > 0}
        mono={false}
      />
      <span className="sr-only">
        {count} waiting{overdue > 0 && `, ${overdue} for a day or more`}
      </span>
    </>
  )
}

const ledger = createColumnHelper<LedgerRow>()

const LEDGER_COLUMNS = ledger.columns([
  ledger.accessor("name", {
    header: "Workflow or agent",
    // The column that stretches; this is its narrowest.
    size: 220,
    enableHiding: false,
    meta: { label: "Workflow or agent" },
    cell: ({ row: { original: row } }) => (
      <span className="flex min-w-0 items-center gap-2.5">
        <Mark row={row} />
        <span className="flex min-w-0 flex-col">
          <span
            className={cn(
              "truncate text-[0.8125rem] text-foreground",
              (row.total || row.selected) && "font-[550]"
            )}
          >
            {row.name}
          </span>
          <span className="truncate text-2xs text-muted-foreground">
            {row.detail}
          </span>
        </span>
      </span>
    ),
  }),
  ledger.accessor((row) => (row.kind ? SUBJECT_KINDS[row.kind].label : ""), {
    id: "kind",
    header: "Type",
    size: 96,
    meta: { label: "Type" },
    cell: ({ getValue }) => (
      <span className="text-muted-foreground">{getValue() || "All"}</span>
    ),
  }),
  ledger.accessor("tokens", {
    header: "Tokens",
    size: 176,
    meta: { label: "Tokens", align: "right" },
    cell: ({ row: { original: row } }) => (
      <span className="flex items-center justify-end gap-3.5">
        <WeekBars weeks={row.weeks} peak={row.peak} />
        <Figure
          total={row.total}
          value={compact(row.tokens)}
          change={
            <VolumeDelta current={row.tokens} previous={row.tokensBefore} />
          }
        />
      </span>
    ),
  }),
  ledger.accessor("runs", {
    header: "Runs",
    size: 112,
    meta: { label: "Runs and invocations", align: "right" },
    cell: ({ row: { original: row } }) => (
      <Figure
        total={row.total}
        value={row.runs.toLocaleString()}
        change={<VolumeDelta current={row.runs} previous={row.runsBefore} />}
      />
    ),
  }),
  ledger.accessor((row) => row.success ?? undefined, {
    id: "success",
    header: "Succeeded",
    size: 120,
    sortUndefined: "last",
    meta: { label: "Succeeded", align: "right" },
    cell: ({ row: { original: row } }) => (
      <Figure
        total={row.total}
        value={row.success === null ? null : percent(row.success)}
        change={
          <Delta
            current={row.success}
            previous={row.successBefore}
            better="higher"
            threshold={0.02}
            format={(change) => `${Math.round(Math.abs(change) * 100)} pts`}
          />
        }
      />
    ),
  }),
  ledger.accessor("calls", {
    header: "Model calls",
    size: 112,
    meta: { label: "Model calls", align: "right" },
    cell: ({ row: { original: row } }) => (
      <span className={cn(row.total && "font-[550]")}>
        {row.calls.toLocaleString()}
      </span>
    ),
  }),
  ledger.accessor("cost", {
    header: "Spend",
    size: 120,
    meta: { label: "Spend", align: "right" },
    cell: ({ row: { original: row } }) => (
      <Figure
        total={row.total}
        value={usd(row.cost)}
        change={
          row.costPerRun === null ? "No runs" : `${usd(row.costPerRun)} a run`
        }
      />
    ),
  }),
  ledger.accessor((row) => row.averageMs ?? undefined, {
    id: "average",
    header: "Average time",
    size: 112,
    sortUndefined: "last",
    meta: { label: "Average time", align: "right" },
    cell: ({ row: { original: row } }) =>
      row.averageMs === null ? (
        <span className="text-muted-foreground">None</span>
      ) : (
        <span className={cn(row.total && "font-[550]")}>
          {duration(row.averageMs)}
        </span>
      ),
  }),
  ledger.accessor("waiting", {
    header: "Waiting on people",
    size: 140,
    meta: { label: "Waiting on people", align: "left" },
    cell: ({ row: { original: row } }) => (
      <Waiting count={row.waiting} overdue={row.overdue} />
    ),
  }),
])

export function Ledger({
  rows,
  period,
  toolbar,
  onSelect,
}: {
  rows: LedgerRow[]
  period: OverviewPeriod
  /** Controls beside the table's own, such as the type switch. */
  toolbar?: React.ReactNode
  onSelect: (id: string) => void
}) {
  return (
    <>
      <DataTable
        title="Workflows and agents"
        description={`The last ${OVERVIEW_PERIODS[period].label}, each workflow and agent beside the rest. Select one to focus everything below on it.`}
        columns={LEDGER_COLUMNS}
        data={rows}
        getRowId={(row) => row.id}
        features={LEDGER_FEATURES}
        pinningColumn={false}
        tableOptions={PIN_SCOPE}
        rowClassName={selectedRow}
        onRowClick={(row) => onSelect(row.original.id)}
        toolbarActions={toolbar}
        stateKey="organization-overview-ledger"
        exportFileName="organization-usage"
        // Phones get the stacked ledger below instead.
        className="@max-[600px]/shell:hidden"
      />
      <div className="hidden @max-[600px]/shell:block">
        {toolbar && <div className="mb-3">{toolbar}</div>}
        <LedgerStack rows={rows} onSelect={onSelect} />
      </div>
    </>
  )
}

/**
 * The ledger on phones: each row stacked, its figures in a two-column grid,
 * so every workflow and agent still compares on the same figures.
 */
function LedgerStack({
  rows,
  onSelect,
}: {
  rows: LedgerRow[]
  onSelect: (id: string) => void
}) {
  return (
    <ul
      aria-label="Workflows and agents"
      className="overflow-hidden rounded-(--radius-band) border"
    >
      {rows.map((row) => (
        <li
          key={row.id}
          data-selected={row.selected || undefined}
          onClick={() => onSelect(row.id)}
          className="cursor-pointer border-b px-3.5 py-3 transition-colors last:border-b-0 hover:bg-[color-mix(in_oklch,var(--muted)_50%,var(--background))] data-selected:bg-muted"
        >
          <div className="flex items-center gap-3">
            <button
              type="button"
              aria-pressed={row.selected}
              aria-controls="overview-scope"
              onClick={(event) => {
                event.stopPropagation()
                onSelect(row.id)
              }}
              className="flex min-w-0 flex-1 items-center gap-2.5 text-left"
            >
              <Mark row={row} />
              <span className="min-w-0">
                <span
                  className={cn(
                    "block truncate text-sm",
                    row.selected || row.total ? "font-[550]" : "font-normal"
                  )}
                >
                  {row.name}
                </span>
                <span className="block truncate text-[0.8125rem]/[1.4] text-muted-foreground">
                  {row.detail}
                </span>
              </span>
            </button>
            <WeekBars weeks={row.weeks} peak={row.peak} />
          </div>
          <dl className="mt-3 grid grid-cols-2 gap-x-4 gap-y-2.5 pl-[25px] text-xs tabular-nums">
            <StackFigure label="Tokens">{compact(row.tokens)}</StackFigure>
            <StackFigure label="Spend">{usd(row.cost)}</StackFigure>
            <StackFigure label="Runs">{row.runs.toLocaleString()}</StackFigure>
            <StackFigure label="Succeeded">
              {row.success === null ? "None" : percent(row.success)}
            </StackFigure>
            <StackFigure label="Model calls">
              {row.calls.toLocaleString()}
            </StackFigure>
            <StackFigure label="Waiting on people">
              <Waiting count={row.waiting} overdue={row.overdue} />
            </StackFigure>
          </dl>
        </li>
      ))}
    </ul>
  )
}

function StackFigure({
  label,
  children,
}: {
  label: string
  children: React.ReactNode
}) {
  return (
    <div className="min-w-0">
      <dt className="text-2xs text-muted-foreground">{label}</dt>
      <dd className="mt-0.5 font-medium">{children}</dd>
    </div>
  )
}
