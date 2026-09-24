import type { RowData } from "@tanstack/react-table"

import { Checkbox } from "@/components/ui/checkbox"
import {
  ExpandButton,
  RowDragHandle,
  RowNumber,
  RowPinMenu,
  SelectAllCheckbox,
} from "./cells"
import type { ColumnDef } from "./table-config"
import type { DataTableLabels } from "./types"

// Leading columns DataTable adds for selection, numbering, dragging,
// expanding and row pinning. Ids start with "__" (see isUtilityColumnId).

const utilityColumn = {
  size: 44,
  minSize: 44,
  maxSize: 44,
  enableSorting: false,
  enableHiding: false,
  enableGrouping: false,
  enableResizing: false,
  enablePinning: false,
  enableColumnFilter: false,
  enableGlobalFilter: false,
  enableCellSelection: false,
  meta: {
    align: "center" as const,
    className: "px-0",
    filterVariant: "none" as const,
  },
}

export function createSelectionColumn<TData extends RowData>(
  labels: DataTableLabels
): ColumnDef<TData, unknown> {
  return {
    ...utilityColumn,
    id: "__select",
    header: SelectAllCheckbox as ColumnDef<TData, unknown>["header"],
    cell: ({ row }) => (
      <Checkbox
        aria-label={labels.selectRow}
        className="mx-auto"
        checked={row.getIsSelected()}
        disabled={!row.getCanSelect()}
        indeterminate={row.getIsSomeSelected()}
        onCheckedChange={(checked) => row.toggleSelected(checked)}
        onClick={(event) => event.stopPropagation()}
      />
    ),
  }
}

export function createRowNumberColumn<TData extends RowData>(
  labels: DataTableLabels
): ColumnDef<TData, unknown> {
  return {
    ...utilityColumn,
    id: "__row_number",
    size: 52,
    minSize: 52,
    maxSize: 52,
    header: () => labels.rowNumber,
    cell: RowNumber as ColumnDef<TData, unknown>["cell"],
    meta: {
      ...utilityColumn.meta,
      className: "px-0 text-3xs text-subtle tabular-nums",
    },
  }
}

export function createRowDragColumn<TData extends RowData>(): ColumnDef<
  TData,
  unknown
> {
  return {
    ...utilityColumn,
    id: "__row_drag",
    size: 32,
    minSize: 32,
    maxSize: 32,
    cell: RowDragHandle as ColumnDef<TData, unknown>["cell"],
  }
}

export function createExpanderColumn<TData extends RowData>(
  labels: DataTableLabels
): ColumnDef<TData, unknown> {
  return {
    ...utilityColumn,
    id: "__expander",
    cell: ({ row }) =>
      row.getCanExpand() ? (
        <ExpandButton
          expanded={row.getIsExpanded()}
          labels={labels}
          onToggle={() => row.toggleExpanded()}
        />
      ) : null,
  }
}

export function createPinningColumn<TData extends RowData>(
  labels: DataTableLabels
): ColumnDef<TData, unknown> {
  return {
    ...utilityColumn,
    id: "__row_pin",
    cell: ({ row }) =>
      row.getCanPin() ? <RowPinMenu row={row} labels={labels} /> : null,
  }
}
