import * as React from "react"
import { cn } from "cn"
import { flexRender, type RowData } from "@tanstack/react-table"

import { Button } from "@/components/ui/button"
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { TableHead } from "@/components/ui/table"
import { Icon } from "../icon"
import { useDataTable } from "./context"
import { getPinnedClass, getPinnedStyles } from "./layout"
import type { Column, Header, Table } from "./table-config"
import { getAlignClass, getColumnLabel, isUtilityColumnId } from "./utils"

/** Moves `columnId` next to `targetId` in the column order. */
function reorderColumn(
  table: Table<RowData>,
  columnId: string,
  targetId: string,
  position: "before" | "after"
) {
  const order = table.atoms.columnOrder.get().length
    ? [...table.atoms.columnOrder.get()]
    : table.getAllLeafColumns().map((column) => column.id)
  const from = order.indexOf(columnId)
  if (from < 0) return
  order.splice(from, 1)
  const target = order.indexOf(targetId)
  if (target < 0) return
  order.splice(position === "before" ? target : target + 1, 0, columnId)
  table.setColumnOrder(order)
}

function moveColumn(
  table: Table<RowData>,
  columnId: string,
  direction: "left" | "right"
) {
  const order = table.atoms.columnOrder.get().length
    ? [...table.atoms.columnOrder.get()]
    : table.getAllLeafColumns().map((column) => column.id)
  const index = order.indexOf(columnId)
  const next = direction === "left" ? index - 1 : index + 1
  if (index < 0 || next < 0 || next >= order.length) return
  if (isUtilityColumnId(order[next])) return
  const [id] = order.splice(index, 1)
  order.splice(next, 0, id)
  table.setColumnOrder(order)
}

function ColumnMenu({ column }: { column: Column<RowData, unknown> }) {
  const { table, features, labels } = useDataTable()
  const canSort = features.sorting && column.getCanSort()
  const canPin = features.columnPinning && column.getCanPin()
  const canGroup = features.grouping && column.getCanGroup()
  const canHide = features.columnVisibility && column.getCanHide()
  const canOrder = features.columnOrdering && !isUtilityColumnId(column.id)
  const canResize = features.columnSizing && column.getCanResize()
  const sorted = column.getIsSorted()
  const pinned = column.getIsPinned()

  if (!canSort && !canPin && !canGroup && !canHide && !canOrder) return null

  return (
    <DropdownMenu>
      <DropdownMenuTrigger
        render={
          <Button
            type="button"
            variant="ghost"
            size="icon-xs"
            className="text-subtle opacity-0 group-hover/th:opacity-100 focus-visible:opacity-100 aria-expanded:opacity-100 pointer-coarse:opacity-100"
            aria-label={`${labels.tableOptions}: ${getColumnLabel(column)}`}
          />
        }
      >
        <Icon icon="more" />
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-52">
        <DropdownMenuGroup>
          <DropdownMenuLabel>{getColumnLabel(column)}</DropdownMenuLabel>
          {canSort ? (
            <>
              <DropdownMenuItem
                disabled={sorted === "asc"}
                onClick={() => column.toggleSorting(false)}
              >
                <Icon icon="up" className="text-muted-foreground" />
                {labels.sortAscending}
              </DropdownMenuItem>
              <DropdownMenuItem
                disabled={sorted === "desc"}
                onClick={() => column.toggleSorting(true)}
              >
                <Icon icon="down" className="text-muted-foreground" />
                {labels.sortDescending}
              </DropdownMenuItem>
              {sorted ? (
                <DropdownMenuItem onClick={() => column.clearSorting()}>
                  <Icon icon="close" className="text-muted-foreground" />
                  {labels.clearSort}
                </DropdownMenuItem>
              ) : null}
            </>
          ) : null}
          {canGroup ? (
            <DropdownMenuCheckboxItem
              checked={column.getIsGrouped()}
              onCheckedChange={() => column.toggleGrouping()}
            >
              <Icon icon="layers" className="text-muted-foreground" />
              {labels.grouping}
            </DropdownMenuCheckboxItem>
          ) : null}
        </DropdownMenuGroup>
        {canOrder ? (
          <>
            <DropdownMenuSeparator />
            <DropdownMenuGroup>
              <DropdownMenuItem
                onClick={() => moveColumn(table, column.id, "left")}
              >
                <Icon icon="left" className="text-muted-foreground" />
                {labels.moveLeft}
              </DropdownMenuItem>
              <DropdownMenuItem
                onClick={() => moveColumn(table, column.id, "right")}
              >
                <Icon icon="right" className="text-muted-foreground" />
                {labels.moveRight}
              </DropdownMenuItem>
            </DropdownMenuGroup>
          </>
        ) : null}
        {canPin ? (
          <>
            <DropdownMenuSeparator />
            <DropdownMenuGroup>
              <DropdownMenuItem
                disabled={pinned === "start"}
                onClick={() => column.pin("start")}
              >
                <Icon icon="pin" className="text-muted-foreground" />
                {labels.pinLeft}
              </DropdownMenuItem>
              <DropdownMenuItem
                disabled={pinned === "end"}
                onClick={() => column.pin("end")}
              >
                <Icon icon="pin" className="text-muted-foreground" />
                {labels.pinRight}
              </DropdownMenuItem>
              <DropdownMenuItem
                disabled={!pinned}
                onClick={() => column.pin(false)}
              >
                <Icon icon="close" className="text-muted-foreground" />
                {labels.unpin}
              </DropdownMenuItem>
            </DropdownMenuGroup>
          </>
        ) : null}
        {canResize || canHide ? (
          <>
            <DropdownMenuSeparator />
            <DropdownMenuGroup>
              {canResize ? (
                <DropdownMenuItem onClick={() => column.resetSize()}>
                  <Icon icon="refresh" className="text-muted-foreground" />
                  {labels.resetWidth}
                </DropdownMenuItem>
              ) : null}
              {canHide ? (
                <DropdownMenuItem
                  onClick={() => column.toggleVisibility(false)}
                >
                  <Icon icon="view" className="text-muted-foreground" />
                  {labels.hideColumn}
                </DropdownMenuItem>
              ) : null}
            </DropdownMenuGroup>
          </>
        ) : null}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

/** Sortable, draggable label with sort order and filter indicators. */
function ColumnLabel({ header }: { header: Header<RowData, unknown> }) {
  const { table, features, setDraggingColumnId } = useDataTable()
  const column = header.column
  const sorted = column.getIsSorted()
  const canSort = features.sorting && column.getCanSort()
  const align = column.columnDef.meta?.align
  const content = flexRender(column.columnDef.header, header.getContext())
  const sortCount = table.atoms.sorting.get().length
  const sortIndex = column.getSortIndex()
  const canDrag =
    features.columnDragging &&
    features.columnOrdering &&
    !isUtilityColumnId(column.id) &&
    !column.getIsPinned()

  return (
    <div
      className={cn(
        "flex w-full min-w-0 items-center gap-1",
        align === "right" && "flex-row-reverse",
        align === "center" && "justify-center",
        canDrag && "cursor-grab active:cursor-grabbing"
      )}
      draggable={canDrag || undefined}
      onDragStart={(event) => {
        event.dataTransfer.setData("text/plain", column.id)
        event.dataTransfer.effectAllowed = "move"
        setDraggingColumnId(column.id)
      }}
      onDragEnd={() => setDraggingColumnId(null)}
    >
      {canSort ? (
        <button
          type="button"
          className={cn(
            "flex min-w-0 items-center gap-1 rounded-(--radius-item) transition-colors hover:text-foreground",
            align === "right" && "flex-row-reverse",
            sorted && "text-foreground"
          )}
          onClick={column.getToggleSortingHandler()}
          title={
            features.multiSort
              ? "Shift-click to sort by several columns"
              : undefined
          }
        >
          <span className="truncate">{content}</span>
          <Icon
            icon={sorted === "asc" ? "up" : sorted === "desc" ? "down" : "sort"}
            size={13}
            className={cn("shrink-0", !sorted && "text-subtle")}
          />
          {sorted && sortCount > 1 ? (
            <span className="text-4xs text-subtle tabular-nums">
              {sortIndex + 1}
            </span>
          ) : null}
        </button>
      ) : (
        <span className="min-w-0 truncate">{content}</span>
      )}
      {column.getIsFiltered() ? (
        <Icon
          icon="filter"
          size={12}
          className="shrink-0 text-focus"
          aria-label="Filtered"
        />
      ) : null}
    </div>
  )
}

/**
 * A header cell: leaf columns get the sortable label, column menu, drag
 * target and resize handle; group columns get a centered caption.
 */
function HeaderCell({
  header,
  isGroupRow,
}: {
  header: Header<RowData, unknown>
  isGroupRow: boolean
}) {
  const { table, features, draggingColumnId, setDraggingColumnId } =
    useDataTable()
  const [dropSide, setDropSide] = React.useState<"before" | "after" | null>(
    null
  )
  const column = header.column
  const meta = column.columnDef.meta
  const isGroup = !header.isPlaceholder && header.subHeaders.length > 0
  const canDrop =
    !header.isPlaceholder &&
    draggingColumnId !== null &&
    draggingColumnId !== column.id &&
    !isGroup &&
    !isUtilityColumnId(column.id) &&
    !column.getIsPinned()

  return (
    <TableHead
      colSpan={header.colSpan}
      data-drop={dropSide ?? undefined}
      aria-sort={
        !isGroup && column.getCanSort()
          ? column.getIsSorted() === "asc"
            ? "ascending"
            : column.getIsSorted() === "desc"
              ? "descending"
              : "none"
          : undefined
      }
      className={cn(
        "group/th relative px-[17px] align-middle text-2xs font-normal text-muted-foreground",
        isGroupRow
          ? "h-[34px] group-data-[density=compact]/data-table:h-7"
          : "h-[45px] group-data-[density=compact]/data-table:h-[34px]",
        isGroup &&
          "border-x border-b text-left font-medium text-foreground first:border-l-0 last:border-r-0",
        !isGroup && getAlignClass(meta?.align),
        isUtilityColumnId(column.id) && "px-0 text-center",
        draggingColumnId === column.id && "opacity-50",
        "data-[drop=after]:shadow-[inset_-2px_0_0_var(--focus)] data-[drop=before]:shadow-[inset_2px_0_0_var(--focus)]",
        getPinnedClass(column),
        meta?.headerClassName,
        meta?.className
      )}
      style={getPinnedStyles(column)}
      onDragOver={(event) => {
        if (!canDrop) return
        event.preventDefault()
        const rect = event.currentTarget.getBoundingClientRect()
        setDropSide(
          event.clientX < rect.left + rect.width / 2 ? "before" : "after"
        )
      }}
      onDragLeave={() => setDropSide(null)}
      onDrop={(event) => {
        if (!canDrop || !draggingColumnId || !dropSide) return
        event.preventDefault()
        reorderColumn(table, draggingColumnId, column.id, dropSide)
        setDropSide(null)
        setDraggingColumnId(null)
      }}
    >
      {header.isPlaceholder ? null : isGroup ? (
        // Sticks beside the pinned columns so wide groups stay labelled.
        <span
          className="sticky inline-block max-w-full truncate align-middle"
          style={{
            insetInlineStart: column.getIsPinned()
              ? undefined
              : table.getStartTotalSize() + 17,
          }}
        >
          {flexRender(column.columnDef.header, header.getContext())}
        </span>
      ) : (
        <>
          <ColumnLabel header={header} />
          {/* Overlaid on the side away from the label so it never truncates
              it; its backing shows only with the menu, so at rest it never
              covers a long label's sort icon either. */}
          <span
            className={cn(
              "absolute top-1/2 flex -translate-y-1/2 rounded-(--radius-control) group-hover/th:bg-background focus-within:bg-background has-aria-expanded:bg-background pointer-coarse:bg-background",
              meta?.align === "right" ? "left-2" : "right-2"
            )}
          >
            <ColumnMenu column={column} />
          </span>
        </>
      )}
      {features.columnSizing &&
      !header.isPlaceholder &&
      column.getCanResize() ? (
        <button
          type="button"
          aria-label={`Resize ${getColumnLabel(column)}`}
          title="Drag to resize, double-click to reset"
          className={cn(
            "absolute top-2 right-0 bottom-2 w-[3px] cursor-col-resize touch-none rounded-full bg-transparent transition-colors select-none group-hover/th:bg-border hover:bg-focus/60",
            column.getIsResizing() && "bg-focus"
          )}
          onMouseDown={header.getResizeHandler()}
          onTouchStart={header.getResizeHandler()}
          onDoubleClick={() => column.resetSize()}
        />
      ) : null}
    </TableHead>
  )
}

export { HeaderCell }
