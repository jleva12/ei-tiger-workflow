import * as React from "react"
import type { RowData } from "@tanstack/react-table"

import { Button } from "@/components/ui/button"
import { Label } from "@/components/ui/label"
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Icon } from "../icon"
import { useDataTable } from "./context"
import type { Table } from "./table-config"
import { formatValue, isDateColumn, selectionStats } from "./utils"

const count = (value: number) => formatValue(value, { format: "integer" })
const stat = (value: number) =>
  formatValue(value, {
    format: "number",
    formatOptions: { maximumFractionDigits: 2 },
  })

/** Selected values from number columns; timestamps in date columns don't add up. */
function numericValues(table: Table<RowData>) {
  const columnIds = table.getCellSelectionColumnIds().filter((id) => {
    const column = table.getColumn(id)
    return column && !isDateColumn(column)
  })
  const values: unknown[] = []
  for (const rowId of table.getCellSelectionRowIds()) {
    const cells = table.getRow(rowId, true)?.getAllCellsByColumnId()
    if (!cells) continue
    for (const columnId of columnIds) {
      const cell = cells[columnId]
      if (cell?.getIsSelected()) values.push(cell.getValue())
    }
  }
  return values
}

/** Count / Sum / Avg / Min / Max of the selected cells. */
function SelectionSummary() {
  const { table, features } = useDataTable()
  const selectedCells = features.cellSelection
    ? table.getSelectedCellCount()
    : 0
  if (selectedCells < 2) return null
  // Very large ranges would stall the render; the count is enough there.
  const stats =
    selectedCells <= 100_000 ? selectionStats(numericValues(table)) : undefined
  const items: Array<[string, string]> = [["Count", count(selectedCells)]]
  if (stats && stats.numeric > 0) {
    items.push(
      ["Sum", stat(stats.sum ?? 0)],
      ["Avg", stat(stats.mean ?? 0)],
      ["Min", stat(stats.min ?? 0)],
      ["Max", stat(stats.max ?? 0)]
    )
  }
  return (
    <dl className="flex flex-wrap items-center gap-x-3 gap-y-1 tabular-nums">
      {items.map(([label, value]) => (
        <div key={label} className="flex items-baseline gap-1">
          <dt className="text-subtle">{label}</dt>
          <dd className="text-foreground">{value}</dd>
        </div>
      ))}
    </dl>
  )
}

function Pagination() {
  const { table, labels } = useDataTable()
  const pageSizeId = React.useId()
  const { pageIndex, pageSize } = table.atoms.pagination.get()
  const pageSizes = [...new Set([10, 20, 30, 40, 50, 100, pageSize])].sort(
    (a, b) => a - b
  )
  return (
    <div className="flex flex-wrap items-center gap-[7px]">
      <Label
        className="text-2xs font-normal text-muted-foreground"
        htmlFor={pageSizeId}
      >
        {labels.rowsPerPage}
      </Label>
      <Select<number>
        value={pageSize}
        onValueChange={(value) => {
          if (value !== null) table.setPageSize(value)
        }}
        items={pageSizes.map((value) => ({ value, label: String(value) }))}
      >
        <SelectTrigger id={pageSizeId} size="sm" className="w-[70px] text-xs">
          <SelectValue />
        </SelectTrigger>
        <SelectContent alignItemWithTrigger={false}>
          <SelectGroup>
            {pageSizes.map((value) => (
              <SelectItem key={value} value={value}>
                {value}
              </SelectItem>
            ))}
          </SelectGroup>
        </SelectContent>
      </Select>
      <div className="min-w-24 text-center tabular-nums">
        {labels.page} <span className="text-foreground">{pageIndex + 1}</span>{" "}
        {labels.of} {Math.max(1, table.getPageCount())}
      </div>
      <Button
        type="button"
        variant="outline"
        size="icon-sm"
        aria-label={labels.firstPage}
        onClick={() => table.setPageIndex(0)}
        disabled={!table.getCanPreviousPage()}
      >
        <Icon icon="left" />
      </Button>
      <Button
        type="button"
        variant="outline"
        size="sm"
        onClick={() => table.previousPage()}
        disabled={!table.getCanPreviousPage()}
      >
        {labels.previousPage}
      </Button>
      <Button
        type="button"
        variant="outline"
        size="sm"
        onClick={() => table.nextPage()}
        disabled={!table.getCanNextPage()}
      >
        {labels.nextPage}
      </Button>
      <Button
        type="button"
        variant="outline"
        size="icon-sm"
        aria-label={labels.lastPage}
        onClick={() => table.setPageIndex(table.getPageCount() - 1)}
        disabled={!table.getCanNextPage()}
      >
        <Icon icon="right" />
      </Button>
    </div>
  )
}

/**
 * Row counts, selection, selected-cell statistics and pagination under the
 * grid. `notice` briefly confirms actions such as copying; `actions` are the
 * table's own controls at its end.
 */
function DataTableStatusBar({
  notice,
  actions,
}: {
  notice: string | null
  actions?: React.ReactNode
}) {
  const { table, features, labels, treeData } = useDataTable()
  const paged = features.pagination && !features.virtualization
  const manual = Boolean(table.options.manualFiltering)
  // Tree data counts every node, not just the roots.
  const filtered = table.getFilteredRowModel()
  const total =
    table.options.rowCount ??
    (treeData ? filtered.flatRows.length : filtered.rows.length)
  const core = table.getCoreRowModel()
  const unfiltered = treeData ? core.flatRows.length : core.rows.length
  // Every selected row, sub-rows too: a tree's `rows` holds only those
  // whose parents are selected.
  const selectedRows = features.rowSelection
    ? table.getFilteredSelectedRowModel().flatRows.length
    : 0
  const { pageIndex, pageSize } = table.atoms.pagination.get()
  const pageTotal = table.getRowCount()
  const first = pageTotal ? pageIndex * pageSize + 1 : 0
  const last = Math.min((pageIndex + 1) * pageSize, pageTotal)
  const rowsLabel =
    total === 1 ? labels.rowSingular || labels.rows : labels.rows

  return (
    <div
      data-slot="data-table-pagination"
      className="flex flex-col gap-2.5 border-t px-4 py-2.5 text-2xs text-muted-foreground @2xl/data-table:flex-row @2xl/data-table:items-center @2xl/data-table:justify-between"
    >
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <div className="tabular-nums">
          {features.rowSelection ? (
            <>
              <span className="text-foreground">{count(selectedRows)}</span>{" "}
              {labels.rowsSelected} ·{" "}
            </>
          ) : null}
          {paged && pageTotal > pageSize ? (
            <>
              {labels.showing}{" "}
              <span className="text-foreground">
                {count(first)}–{count(last)}
              </span>{" "}
              {labels.of}{" "}
            </>
          ) : null}
          <span className="text-foreground">{count(total)}</span> {rowsLabel}
          {!manual && total < unfiltered ? (
            <span className="text-subtle">
              {" "}
              ({labels.of} {count(unfiltered)})
            </span>
          ) : null}
        </div>
        {features.statusBar ? <SelectionSummary /> : null}
        <span role="status" aria-live="polite" className="text-foreground">
          {notice ? (
            <span className="inline-flex items-center gap-1">
              <Icon
                icon="check"
                size={12}
                className="text-success-foreground"
              />
              {notice}
            </span>
          ) : null}
        </span>
      </div>
      {actions ? (
        <div className="flex flex-wrap items-center gap-2">{actions}</div>
      ) : null}
      {paged ? <Pagination /> : null}
    </div>
  )
}

export { DataTableStatusBar }
