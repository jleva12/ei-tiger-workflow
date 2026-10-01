import * as React from "react"
import { cn } from "cn"
import { flexRender, type RowData } from "@tanstack/react-table"
import { useVirtualizer } from "@tanstack/react-virtual"

import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Skeleton } from "@/components/ui/skeleton"
import { TableBody, TableCell, TableRow } from "@/components/ui/table"
import { Icon } from "../icon"
import { Illustration } from "../illustration"
import { CountBadge } from "../status"
import { CellEditor } from "./cell-editor"
import { ExpandButton } from "./cells"
import { useDataTable } from "./context"
import {
  cellHeight,
  getPinnedClass,
  getPinnedStyles,
  rowHeightPx,
  subtleSurface,
} from "./layout"
import type { Cell, Row } from "./table-config"
import {
  getAlignClass,
  getColumnLabel,
  inferEditor,
  isUtilityColumnId,
} from "./utils"

type DropTarget = { rowId: string; position: "before" | "after" }

export type DataTableBodyProps = {
  scrollRef: React.RefObject<HTMLDivElement | null>
  emptyState?: React.ReactNode
  isLoading: boolean
  skeletonRows: number
  onRowClick?: (row: Row<RowData>) => void
  onRowDoubleClick?: (row: Row<RowData>) => void
  renderSubComponent?: (row: Row<RowData>) => React.ReactNode
  rowClassName?: string | ((row: Row<RowData>) => string)
}

const interactiveSelector =
  "button, a, input, textarea, select, label, [role='checkbox'], [role='combobox'], [role='button']"

/** Clicks from portals (menus) or on controls inside a row aren't row clicks. */
function isForeignTarget(event: React.SyntheticEvent) {
  const target = event.target
  return (
    target instanceof Element &&
    (!event.currentTarget.contains(target) ||
      target.closest(interactiveSelector) !== null)
  )
}

/* Range selection is drawn on a pseudo-element so pinned cells keep their surface. */
const selectionClass =
  "after:pointer-events-none after:absolute after:inset-0 after:border-focus focus-visible:outline-none! data-selected:after:bg-[color-mix(in_oklch,var(--focus)_12%,transparent)] data-edge-top:after:border-t-2 data-edge-right:after:border-r-2 data-edge-bottom:after:border-b-2 data-edge-left:after:border-l-2 data-focused:after:outline-2 data-focused:after:-outline-offset-2 data-focused:after:outline-focus"

const dropClass =
  "group-data-[drop=before]/row:shadow-[inset_0_2px_0_var(--focus)] group-data-[drop=after]/row:shadow-[inset_0_-2px_0_var(--focus)]"

function CellContent({
  cell,
  isEditing,
}: {
  cell: Cell<RowData, unknown>
  isEditing: boolean
}) {
  const { labels, treeColumnId, treeData, canEdit, commitEdit } = useDataTable()
  const row = cell.row
  const column = cell.column

  if (isEditing) return <CellEditor row={row} column={column} />

  if (cell.getIsGrouped()) {
    return (
      <Button
        variant="ghost"
        size="sm"
        type="button"
        className="-ml-2 max-w-full gap-1.5 font-medium group-data-[density=compact]/data-table:h-6"
        aria-expanded={row.getIsExpanded()}
        onClick={row.getToggleExpandedHandler()}
      >
        <Icon
          icon="right"
          className={cn(
            "text-muted-foreground transition-transform duration-150",
            row.getIsExpanded() && "rotate-90"
          )}
        />
        <span className="truncate">
          {flexRender(column.columnDef.cell, cell.getContext())}
        </span>
        <CountBadge>{row.subRows.length}</CountBadge>
      </Button>
    )
  }

  if (cell.getIsAggregated()) {
    return flexRender(
      column.columnDef.aggregatedCell ?? column.columnDef.cell,
      cell.getContext()
    )
  }

  // Group rows show only the group key and aggregates, never a leaf's data.
  if (
    cell.getIsPlaceholder() ||
    (row.getIsGrouped() && !isUtilityColumnId(column.id))
  ) {
    return null
  }

  const meta = column.columnDef.meta
  const value = cell.getValue()
  const isCheckboxEditor =
    canEdit(row, column.id) &&
    (meta?.editor ?? inferEditor(value, Boolean(meta?.editorOptions))) ===
      "checkbox"

  const content = isCheckboxEditor ? (
    <Checkbox
      aria-label={`Edit ${getColumnLabel(column)}`}
      className={cn(
        meta?.align === "center" && "mx-auto",
        meta?.align === "right" && "ml-auto"
      )}
      checked={Boolean(value)}
      onCheckedChange={(checked) => commitEdit(row, column.id, checked)}
    />
  ) : (
    flexRender(column.columnDef.cell, cell.getContext())
  )

  if (treeData && column.id === treeColumnId) {
    return (
      <span
        className="flex min-w-0 items-center gap-1"
        style={{ paddingInlineStart: row.depth * 20 }}
      >
        {row.getCanExpand() ? (
          <ExpandButton
            className="-ml-1.5 shrink-0"
            expanded={row.getIsExpanded()}
            labels={labels}
            onToggle={() => row.toggleExpanded()}
          />
        ) : (
          <span className="-ml-1.5 size-6 shrink-0" />
        )}
        <span className="min-w-0 truncate">{content}</span>
      </span>
    )
  }

  return content
}

function BodyCell({
  cell,
  spanning,
  showRange,
}: {
  cell: Cell<RowData, unknown>
  spanning: boolean
  showRange: boolean
}) {
  const { features, editing, onRowReorder } = useDataTable()

  if (spanning && cell.getIsCovered()) return null

  const row = cell.row
  const column = cell.column
  const meta = column.columnDef.meta
  const isEditing =
    editing !== null &&
    editing.rowId === row.id &&
    editing.columnId === column.id
  const selectable =
    features.cellSelection &&
    !isUtilityColumnId(column.id) &&
    cell.getCanSelect()
  const selected = selectable && cell.getIsSelected()
  const edges = selected && showRange ? cell.getSelectionEdges() : undefined
  const rowSpan = spanning ? cell.getRowSpan() : 1
  const colSpan = spanning ? cell.getColSpan() : 1
  const cellClassName =
    typeof meta?.cellClassName === "function"
      ? meta.cellClassName(row, cell.getValue())
      : meta?.cellClassName

  return (
    <TableCell
      data-row-id={row.id}
      data-column-id={column.id}
      data-cell-id={selectable ? cell.id : undefined}
      data-selected={(selected && showRange) || undefined}
      data-focused={(selectable && cell.getIsFocused()) || undefined}
      data-edge-top={edges?.top || undefined}
      data-edge-right={edges?.right || undefined}
      data-edge-bottom={edges?.bottom || undefined}
      data-edge-left={edges?.left || undefined}
      aria-selected={selectable ? selected : undefined}
      tabIndex={selectable ? cell.getTabIndex() : undefined}
      rowSpan={rowSpan > 1 ? rowSpan : undefined}
      colSpan={colSpan > 1 ? colSpan : undefined}
      className={cn(
        "px-[17px] align-middle text-xs",
        cellHeight,
        getAlignClass(meta?.align),
        isEditing
          ? "overflow-visible py-0 group-data-[density=compact]/data-table:py-0"
          : meta?.wrap
            ? "break-words whitespace-normal"
            : "truncate",
        rowSpan > 1 && "bg-background align-top",
        getPinnedClass(column, "body"),
        selectable && selectionClass,
        onRowReorder && dropClass,
        meta?.className,
        cellClassName
      )}
      style={getPinnedStyles(column)}
      onMouseDown={
        selectable && !isEditing
          ? (event) => {
              // Right-clicking inside the selection keeps it for the menu.
              if (event.button === 2 && cell.getIsSelected()) return
              if (event.button !== 0 && event.button !== 2) return
              if (isForeignTarget(event)) return
              if (event.shiftKey) event.preventDefault()
              cell.getSelectionStartHandler()(event)
            }
          : undefined
      }
      onMouseEnter={selectable ? cell.getSelectionExtendHandler() : undefined}
    >
      <CellContent cell={cell} isEditing={isEditing} />
    </TableCell>
  )
}

function BodyRow({
  row,
  columnCount,
  dropPosition,
  index,
  measureRef,
  onDropTargetChange,
  onRowClick,
  onRowDoubleClick,
  renderSubComponent,
  rowClassName,
  showRange,
  spanning,
}: {
  row: Row<RowData>
  columnCount: number
  dropPosition?: DropTarget["position"]
  index?: number
  measureRef?: (element: HTMLTableRowElement | null) => void
  onDropTargetChange: (target: DropTarget | null) => void
  showRange: boolean
  spanning: boolean
} & Pick<
  DataTableBodyProps,
  "onRowClick" | "onRowDoubleClick" | "renderSubComponent" | "rowClassName"
>) {
  const {
    table,
    features,
    draggingRowId,
    setDraggingRowId,
    onRowReorder,
    canEdit,
    startEditing,
  } = useDataTable()
  const pinned = row.getIsPinned()
  const canDropHere =
    Boolean(onRowReorder) && draggingRowId !== null && draggingRowId !== row.id
  const detail =
    renderSubComponent &&
    row.getIsExpanded() &&
    !row.getIsGrouped() &&
    row.subRows.length === 0
      ? renderSubComponent(row)
      : null

  return (
    <>
      <TableRow
        ref={measureRef}
        data-index={index}
        data-row-id={row.id}
        data-state={row.getIsSelected() ? "selected" : undefined}
        data-pinned={pinned || undefined}
        data-drop={dropPosition}
        className={cn(
          // Pinned cells repeat these surfaces (see getPinnedClass) so
          // sticky columns tint with their row.
          "group/row border-b transition-colors hover:bg-[color-mix(in_oklch,var(--muted)_60%,var(--background))] has-aria-expanded:bg-transparent data-[pinned]:bg-[color-mix(in_oklch,var(--muted)_45%,var(--background))] data-[state=selected]:bg-muted",
          draggingRowId === row.id && "opacity-50",
          onRowClick && "cursor-pointer focus-visible:outline-offset-[-2px]!",
          typeof rowClassName === "function" ? rowClassName(row) : rowClassName
        )}
        tabIndex={onRowClick && !features.cellSelection ? 0 : undefined}
        onKeyDown={(event) => {
          if (
            onRowClick &&
            event.target === event.currentTarget &&
            (event.key === "Enter" || event.key === " ")
          ) {
            event.preventDefault()
            onRowClick(row)
          }
        }}
        onClick={(event) => {
          if (onRowClick && !isForeignTarget(event)) onRowClick(row)
        }}
        onDoubleClick={(event) => {
          if (isForeignTarget(event)) return
          const columnId =
            event.target instanceof Element
              ? event.target
                  .closest("td[data-column-id]")
                  ?.getAttribute("data-column-id")
              : null
          if (columnId && canEdit(row, columnId)) {
            startEditing({ rowId: row.id, columnId })
            return
          }
          onRowDoubleClick?.(row)
        }}
        onDragOver={
          canDropHere
            ? (event) => {
                event.preventDefault()
                event.dataTransfer.dropEffect = "move"
                const rect = event.currentTarget.getBoundingClientRect()
                const position =
                  event.clientY < rect.top + rect.height / 2
                    ? "before"
                    : "after"
                if (position !== dropPosition) {
                  onDropTargetChange({ rowId: row.id, position })
                }
              }
            : undefined
        }
        onDrop={
          canDropHere
            ? (event) => {
                event.preventDefault()
                const source = draggingRowId
                  ? table.getRow(draggingRowId, true)
                  : undefined
                if (source && dropPosition) {
                  onRowReorder?.({
                    row: source,
                    targetRow: row,
                    position: dropPosition,
                  })
                }
                onDropTargetChange(null)
                setDraggingRowId(null)
              }
            : undefined
        }
      >
        {row.getVisibleCells().map((cell) => (
          <BodyCell
            key={cell.id}
            cell={cell}
            spanning={spanning}
            showRange={showRange}
          />
        ))}
      </TableRow>
      {detail ? (
        <TableRow
          data-slot="data-table-detail"
          className={cn(
            "border-b hover:bg-transparent has-aria-expanded:bg-transparent",
            subtleSurface
          )}
        >
          <TableCell
            colSpan={columnCount}
            className="px-[17px] py-3.5 whitespace-normal"
          >
            {detail}
          </TableCell>
        </TableRow>
      ) : null}
    </>
  )
}

function SpacerRow({
  height,
  columnCount,
  ref,
}: {
  height: number
  columnCount: number
  ref?: React.Ref<HTMLTableRowElement>
}) {
  return (
    <tr ref={ref} aria-hidden="true">
      <td colSpan={columnCount} className="border-0 p-0" style={{ height }} />
    </tr>
  )
}

/**
 * Body rows: pinned top rows, then the center rows (virtualized when
 * enabled), then pinned bottom rows. Also renders loading and empty states.
 */
function DataTableBody({
  scrollRef,
  emptyState,
  isLoading,
  skeletonRows,
  onRowClick,
  onRowDoubleClick,
  renderSubComponent,
  rowClassName,
}: DataTableBodyProps) {
  const { table, features, labels, density, editing, draggingRowId } =
    useDataTable()
  const [dropTarget, setDropTarget] = React.useState<DropTarget | null>(null)
  const activeDrop = draggingRowId ? dropTarget : null

  const topRows = features.rowPinning ? table.getTopRows() : []
  const centerRows = features.rowPinning
    ? table.getCenterRows()
    : table.getRowModel().rows
  const bottomRows = features.rowPinning ? table.getBottomRows() : []
  const columnCount = Math.max(1, table.getVisibleLeafColumns().length)
  const hasRows = topRows.length + centerRows.length + bottomRows.length > 0
  const spanning = table.options.enableCellSpanning === true
  const showRange = features.cellSelection && table.getSelectedCellCount() > 1

  // Virtual rows start below the header and pinned top rows; the top
  // spacer's offset is that distance.
  const spacerRef = React.useRef<HTMLTableRowElement>(null)
  const [scrollMargin, setScrollMargin] = React.useState(0)
  // Measured after every render (header rows, filters and pinned rows can
  // all move it); the guard stops once the offset settles.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  React.useLayoutEffect(() => {
    const offset = spacerRef.current?.offsetTop ?? 0
    if (Math.abs(offset - scrollMargin) > 0.5) setScrollMargin(offset)
  })

  const virtualizer = useVirtualizer({
    count: centerRows.length,
    enabled: features.virtualization,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => rowHeightPx[density],
    getItemKey: (index) => centerRows[index]?.id ?? index,
    initialRect: { width: 0, height: 600 },
    overscan: 12,
    scrollMargin,
    // Detail rows are measured with the row they belong to.
    measureElement: (element) => {
      let height = element.getBoundingClientRect().height
      const next = element.nextElementSibling
      if (
        next instanceof HTMLElement &&
        next.dataset.slot === "data-table-detail"
      ) {
        height += next.getBoundingClientRect().height
      }
      return height
    },
  })

  // Keyboard navigation moves the focused cell; keep DOM focus on it,
  // scrolling virtual rows into view first.
  const focusedCell = features.cellSelection
    ? table.getFocusedCell()
    : undefined
  const focusedId = focusedCell?.id
  const focusedRowId = focusedCell?.row.id
  React.useEffect(() => {
    const container = scrollRef.current
    if (!container || !focusedId || editing) return
    const active = document.activeElement
    const gridOwnsFocus =
      active === document.body ||
      active === container ||
      (active instanceof HTMLTableCellElement && container.contains(active))
    if (!gridOwnsFocus) return
    const selector = `td[data-cell-id="${CSS.escape(focusedId)}"]`
    const element = container.querySelector<HTMLElement>(selector)
    if (element) {
      if (element !== active) element.focus()
      return
    }
    if (!features.virtualization) return
    const index = centerRows.findIndex((row) => row.id === focusedRowId)
    if (index < 0) return
    virtualizer.scrollToIndex(index)
    const frame = requestAnimationFrame(() =>
      container.querySelector<HTMLElement>(selector)?.focus()
    )
    return () => cancelAnimationFrame(frame)
    // Runs when the focused cell or edit state changes, not on every render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focusedId, editing])

  const rowProps = {
    columnCount,
    onDropTargetChange: setDropTarget,
    onRowClick,
    onRowDoubleClick,
    renderSubComponent,
    rowClassName,
    showRange,
    spanning,
  }
  const renderRow = (
    row: Row<RowData>,
    index?: number,
    measureRef?: (element: HTMLTableRowElement | null) => void
  ) => (
    <BodyRow
      key={row.id}
      row={row}
      index={index}
      measureRef={measureRef}
      dropPosition={
        activeDrop?.rowId === row.id ? activeDrop.position : undefined
      }
      {...rowProps}
    />
  )

  if (isLoading && !hasRows) {
    return (
      <TableBody>
        {Array.from({ length: skeletonRows }).map((_, rowIndex) => (
          <TableRow key={rowIndex} className="border-b hover:bg-transparent">
            {Array.from({ length: columnCount }).map((__, columnIndex) => (
              <TableCell
                key={columnIndex}
                className={cn("px-[17px]", cellHeight)}
              >
                <Skeleton className="h-3 rounded-(--radius-chip)" />
              </TableCell>
            ))}
          </TableRow>
        ))}
      </TableBody>
    )
  }

  if (!hasRows) {
    return (
      <TableBody>
        <TableRow className="hover:bg-transparent">
          <TableCell colSpan={columnCount} className="h-36 px-4 text-center">
            {emptyState ?? (
              <span className="inline-flex flex-col items-center gap-2.5 py-4 text-xs text-subtle">
                <Illustration name="search" size="sm" />
                {labels.noResults}
              </span>
            )}
          </TableCell>
        </TableRow>
      </TableBody>
    )
  }

  const virtualItems = features.virtualization
    ? virtualizer.getVirtualItems()
    : []
  const first = virtualItems[0]
  const last = virtualItems[virtualItems.length - 1]
  const paddingTop = first ? first.start - scrollMargin : 0
  const paddingBottom = last
    ? virtualizer.getTotalSize() - (last.end - scrollMargin)
    : 0

  return (
    <TableBody
      className={cn(
        features.cellSelection && "select-none",
        isLoading && "opacity-60 transition-opacity"
      )}
    >
      {topRows.map((row) => renderRow(row))}
      {features.virtualization ? (
        <>
          <SpacerRow
            ref={spacerRef}
            height={paddingTop}
            columnCount={columnCount}
          />
          {virtualItems.map((item) => {
            const row = centerRows[item.index]
            return row
              ? renderRow(row, item.index, virtualizer.measureElement)
              : null
          })}
          <SpacerRow height={paddingBottom} columnCount={columnCount} />
        </>
      ) : (
        centerRows.map((row) => renderRow(row))
      )}
      {bottomRows.map((row) => renderRow(row))}
    </TableBody>
  )
}

export { DataTableBody }
