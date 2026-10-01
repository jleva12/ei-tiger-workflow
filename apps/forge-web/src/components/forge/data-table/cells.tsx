import { cn } from "cn"
import type { RowData } from "@tanstack/react-table"

import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { Icon } from "../icon"
import { useDataTable } from "./context"
import type { CellContext, HeaderContext, Row } from "./table-config"
import type { DataTableLabels } from "./types"
import {
  formatValue,
  isUtilityColumnId,
  summarize,
  summaryLabels,
} from "./utils"

/* -------------------------------------------------------------------------- */
/* Defaults for every column                                                  */
/* -------------------------------------------------------------------------- */

/** Header text: `meta.label`, else the column id. */
export function DefaultHeader({ column }: HeaderContext<RowData, unknown>) {
  if (isUtilityColumnId(column.id)) return null
  return column.columnDef.meta?.label ?? column.id
}

/** Formats values with `meta.format`; booleans render as a check. */
export function DefaultCell({
  column,
  getValue,
  row,
}: CellContext<RowData, unknown>) {
  const value = getValue()
  const meta = column.columnDef.meta
  if (typeof meta?.format === "function") return meta.format(value, row)
  if (typeof value === "boolean") {
    return value ? (
      <Icon icon="check" size={14} className="inline-block" aria-label="Yes" />
    ) : (
      <span className="text-subtle">
        —<span className="sr-only">No</span>
      </span>
    )
  }
  return formatValue(value, meta)
}

/** Renders `meta.summary` over the filtered rows. */
export function DefaultFooter({
  column,
  table,
}: HeaderContext<RowData, unknown>) {
  const meta = column.columnDef.meta
  const kind = meta?.summary
  if (!kind) return null
  const values = table
    .getFilteredRowModel()
    .rows.map((row) => row.getValue(column.id))
  const result = summarize(values, kind)
  const text =
    result === undefined
      ? "—"
      : kind === "count"
        ? formatValue(result, { format: "integer" })
        : typeof meta.format === "string"
          ? formatValue(result, meta)
          : formatValue(result, {
              format: "number",
              formatOptions: { maximumFractionDigits: 2 },
            })
  // Narrow columns keep the number and move the label to a tooltip.
  return (
    <span
      className="inline-flex items-baseline gap-1.5"
      title={`${summaryLabels[kind]}: ${text}`}
    >
      {column.getSize() >= 150 ? (
        <span className="text-3xs text-subtle uppercase">
          {summaryLabels[kind]}
        </span>
      ) : (
        <span className="sr-only">{summaryLabels[kind]}</span>
      )}
      <span className="text-foreground tabular-nums">{text}</span>
    </span>
  )
}

/** The chevron used by detail rows and tree data. */
export function ExpandButton({
  expanded,
  labels,
  onToggle,
  className,
}: {
  expanded: boolean
  labels: DataTableLabels
  onToggle: () => void
  className?: string
}) {
  return (
    <Button
      type="button"
      variant="ghost"
      size="icon-xs"
      className={cn("text-muted-foreground", className)}
      aria-label={expanded ? labels.collapse : labels.expand}
      aria-expanded={expanded}
      onClick={(event) => {
        // Expand without also triggering onRowClick.
        event.stopPropagation()
        onToggle()
      }}
    >
      <Icon
        icon="right"
        className={cn(
          "transition-transform duration-150",
          expanded && "rotate-90"
        )}
      />
    </Button>
  )
}

/* Utility column cells (see utility-columns.tsx) */

export function SelectAllCheckbox({ table }: HeaderContext<RowData, unknown>) {
  const { features, labels } = useDataTable()
  // Without pages, "all" means every filtered row.
  const paged = features.pagination && !features.virtualization
  return (
    <Checkbox
      aria-label={labels.selectAll}
      className="mx-auto"
      checked={
        paged ? table.getIsAllPageRowsSelected() : table.getIsAllRowsSelected()
      }
      indeterminate={
        paged
          ? table.getIsSomePageRowsSelected()
          : table.getIsSomeRowsSelected()
      }
      onCheckedChange={(checked) =>
        paged
          ? table.toggleAllPageRowsSelected(checked)
          : table.toggleAllRowsSelected(checked)
      }
    />
  )
}

export function RowNumber({ row, table }: CellContext<RowData, unknown>) {
  const { features } = useDataTable()
  if (row.getIsGrouped()) return null
  const { pageIndex, pageSize } = table.atoms.pagination.get()
  const offset =
    features.pagination && !features.virtualization ? pageIndex * pageSize : 0
  const index = row.getDisplayIndex()
  return index < 0 ? null : offset + index + 1
}

export function RowDragHandle({ row, table }: CellContext<RowData, unknown>) {
  const { labels, setDraggingRowId } = useDataTable()
  // Order only means something while rows are unsorted and ungrouped.
  const disabled =
    table.atoms.sorting.get().length > 0 ||
    table.atoms.grouping.get().length > 0 ||
    row.getIsGrouped()
  return (
    <span
      role="button"
      tabIndex={-1}
      aria-label={labels.dragRow}
      aria-disabled={disabled || undefined}
      title={disabled ? undefined : labels.dragRow}
      draggable={!disabled}
      className={cn(
        "mx-auto flex size-6 items-center justify-center rounded-(--radius-item) text-subtle",
        disabled
          ? "opacity-40"
          : "cursor-grab hover:bg-accent hover:text-foreground active:cursor-grabbing"
      )}
      onDragStart={(event) => {
        event.dataTransfer.effectAllowed = "move"
        event.dataTransfer.setData("text/plain", row.id)
        const rowElement = event.currentTarget.closest("tr")
        if (rowElement) event.dataTransfer.setDragImage(rowElement, 20, 16)
        setDraggingRowId(row.id)
      }}
      onDragEnd={() => setDraggingRowId(null)}
    >
      <Icon icon="grip" size={14} />
    </span>
  )
}

export function RowPinMenu<TData extends RowData>({
  row,
  labels,
}: {
  row: Row<TData>
  labels: DataTableLabels
}) {
  return (
    <DropdownMenu>
      <DropdownMenuTrigger
        render={
          <Button
            type="button"
            variant="ghost"
            size="icon-xs"
            className={cn(
              "text-subtle",
              row.getIsPinned() && "text-foreground"
            )}
            aria-label={labels.rowOptions}
          />
        }
      >
        <Icon icon="pin" />
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="min-w-40">
        <DropdownMenuGroup>
          <DropdownMenuItem
            disabled={row.getIsPinned() === "top"}
            onClick={() => row.pin("top")}
          >
            <Icon icon="up" className="text-muted-foreground" />
            {labels.pinTop}
          </DropdownMenuItem>
          <DropdownMenuItem
            disabled={row.getIsPinned() === "bottom"}
            onClick={() => row.pin("bottom")}
          >
            <Icon icon="down" className="text-muted-foreground" />
            {labels.pinBottom}
          </DropdownMenuItem>
        </DropdownMenuGroup>
        <DropdownMenuSeparator />
        <DropdownMenuGroup>
          <DropdownMenuItem
            disabled={!row.getIsPinned()}
            onClick={() => row.pin(false)}
          >
            <Icon icon="close" className="text-muted-foreground" />
            {labels.unpinRow}
          </DropdownMenuItem>
        </DropdownMenuGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
