import type * as React from "react"
import { cn } from "cn"
import type { RowData } from "@tanstack/react-table"

import type { Column, Table } from "./table-config"
import { isUtilityColumnId } from "./utils"

/** The filter row and detail rows share this faint surface. */
export const subtleSurface =
  "bg-[color-mix(in_oklch,var(--muted)_35%,var(--background))]"

/* Row heights follow the task list: 45px, or 34px when compact. */
export const cellHeight =
  "h-[45px] py-2 group-data-[density=compact]/data-table:h-[34px] group-data-[density=compact]/data-table:py-1"

export const rowHeightPx = { default: 45, compact: 34 } as const

// Keep pinned and utility columns at their measured TanStack sizes. One
// unpinned data column fills spare space without shifting sticky offsets.
export function getFlexibleColumn<TData extends RowData>(table: Table<TData>) {
  const sizing = table.atoms.columnSizing.get()
  return table
    .getCenterVisibleLeafColumns()
    .find(
      (column) =>
        !isUtilityColumnId(column.id) && sizing[column.id] === undefined
    )
}

/** Sticky offsets for pinned columns. Widths come from the table's <colgroup>. */
export function getPinnedStyles<TData extends RowData>(
  column: Column<TData, unknown>
): React.CSSProperties {
  const isPinned = column.getIsPinned()
  return {
    insetInlineStart:
      isPinned === "start" ? `${column.getStart("start")}px` : undefined,
    insetInlineEnd:
      isPinned === "end" ? `${column.getAfter("end")}px` : undefined,
    position: isPinned ? "sticky" : "relative",
    zIndex: isPinned ? 3 : 0,
  }
}

/**
 * Pinned cells are sticky, so they paint their own surface: the header's,
 * the filter row's, or the body row's (including hover, selected and pinned).
 */
export function getPinnedClass<TData extends RowData>(
  column: Column<TData, unknown>,
  surface: "head" | "filter" | "body" = "head"
) {
  const isPinned = column.getIsPinned()

  return cn(
    isPinned && surface === "head" && "bg-background",
    isPinned && surface === "filter" && subtleSurface,
    isPinned &&
      surface === "body" &&
      "bg-background group-hover/row:bg-[color-mix(in_oklch,var(--muted)_60%,var(--background))] group-data-[pinned]/row:bg-[color-mix(in_oklch,var(--muted)_45%,var(--background))] group-data-[state=selected]/row:bg-muted",
    isPinned === "start" &&
      column.getIsLastColumn("start") &&
      "border-r shadow-[1px_0_0_var(--border)]",
    isPinned === "end" &&
      column.getIsFirstColumn("end") &&
      "border-l shadow-[-1px_0_0_var(--border)]"
  )
}
