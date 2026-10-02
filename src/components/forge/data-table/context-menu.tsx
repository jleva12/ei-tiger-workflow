import * as React from "react"
import type { RowData } from "@tanstack/react-table"

import {
  ContextMenu,
  ContextMenuContent,
  ContextMenuGroup,
  ContextMenuItem,
  ContextMenuSeparator,
  ContextMenuShortcut,
  ContextMenuTrigger,
} from "@/components/ui/context-menu"
import { Icon } from "../icon"
import { useDataTable } from "./context"
import type { Column, Row, Table } from "./table-config"
import { copyText, getExportText } from "./utils"

export type DataTableContextMenuContext<TData extends RowData> = {
  row: Row<TData>
  column: Column<TData, unknown>
  table: Table<TData>
}

export type DataTableContextMenuItems<TData extends RowData> = (
  context: DataTableContextMenuContext<TData>
) => React.ReactNode

const isMac =
  typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.platform)
const modKey = isMac ? "⌘" : "Ctrl+"

type Target = { rowId: string; columnId: string }

function CellMenuItems({
  target,
  items,
}: {
  target: Target | null
  items?: DataTableContextMenuItems<RowData>
}) {
  const {
    table,
    features,
    labels,
    canEdit,
    startEditing,
    copyRows,
    copySelection,
    exportCsv,
    announce,
  } = useDataTable()
  const row = target ? table.getRow(target.rowId, true) : undefined
  const column = target ? table.getColumn(target.columnId) : undefined
  const hasRange = features.cellSelection && table.getSelectedCellCount() > 1
  const hasSelectedRows =
    features.rowSelection && table.getSelectedRowModel().flatRows.length > 0
  const custom = row && column && items ? items({ row, column, table }) : null

  const cellGroup =
    row && column ? (
      <ContextMenuGroup>
        {hasRange ? (
          <ContextMenuItem onClick={copySelection}>
            <Icon icon="copy" className="text-muted-foreground" />
            {labels.copySelection}
            <ContextMenuShortcut>{modKey}C</ContextMenuShortcut>
          </ContextMenuItem>
        ) : (
          <ContextMenuItem
            onClick={async () => {
              if (await copyText(getExportText(row, column))) {
                announce("Copied cell")
              }
            }}
          >
            <Icon icon="copy" className="text-muted-foreground" />
            {labels.copyCell}
            <ContextMenuShortcut>{modKey}C</ContextMenuShortcut>
          </ContextMenuItem>
        )}
        <ContextMenuItem onClick={() => copyRows([row])}>
          <Icon icon="list" className="text-muted-foreground" />
          {labels.copyRow}
        </ContextMenuItem>
        {canEdit(row, column.id) ? (
          <ContextMenuItem
            onClick={() => startEditing({ rowId: row.id, columnId: column.id })}
          >
            <Icon icon="code" className="text-muted-foreground" />
            Edit cell
            <ContextMenuShortcut>Enter</ContextMenuShortcut>
          </ContextMenuItem>
        ) : null}
      </ContextMenuGroup>
    ) : null

  const rowActions = row
    ? [
        features.rowSelection && row.getCanSelect() ? (
          <ContextMenuItem
            key="select"
            onClick={() => row.toggleSelected(!row.getIsSelected())}
          >
            <Icon icon="check" className="text-muted-foreground" />
            {row.getIsSelected() ? labels.deselectRow : labels.selectRow}
          </ContextMenuItem>
        ) : null,
        features.expanding && row.getCanExpand() ? (
          <ContextMenuItem key="expand" onClick={() => row.toggleExpanded()}>
            <Icon
              icon={row.getIsExpanded() ? "up" : "down"}
              className="text-muted-foreground"
            />
            {row.getIsExpanded() ? labels.collapse : labels.expand}
          </ContextMenuItem>
        ) : null,
        ...(features.rowPinning && row.getCanPin()
          ? [
              row.getIsPinned() !== "top" ? (
                <ContextMenuItem key="top" onClick={() => row.pin("top")}>
                  <Icon icon="pin" className="text-muted-foreground" />
                  {labels.pinTop}
                </ContextMenuItem>
              ) : null,
              row.getIsPinned() !== "bottom" ? (
                <ContextMenuItem key="bottom" onClick={() => row.pin("bottom")}>
                  <Icon icon="pin" className="text-muted-foreground" />
                  {labels.pinBottom}
                </ContextMenuItem>
              ) : null,
              row.getIsPinned() ? (
                <ContextMenuItem key="unpin" onClick={() => row.pin(false)}>
                  <Icon icon="close" className="text-muted-foreground" />
                  {labels.unpinRow}
                </ContextMenuItem>
              ) : null,
            ]
          : []),
      ].filter(Boolean)
    : []

  const sections = [
    cellGroup,
    rowActions.length ? (
      <ContextMenuGroup key="row">{rowActions}</ContextMenuGroup>
    ) : null,
    custom ? <ContextMenuGroup key="custom">{custom}</ContextMenuGroup> : null,
    features.export ? (
      <ContextMenuGroup key="export">
        <ContextMenuItem onClick={() => exportCsv("all")}>
          <Icon icon="download" className="text-muted-foreground" />
          {labels.exportCsv}
        </ContextMenuItem>
        {hasSelectedRows ? (
          <ContextMenuItem onClick={() => exportCsv("selected")}>
            <Icon icon="download" className="text-muted-foreground" />
            {labels.exportSelectedCsv}
          </ContextMenuItem>
        ) : null}
      </ContextMenuGroup>
    ) : null,
  ].filter(Boolean)

  return sections.map((section, index) => (
    <React.Fragment key={index}>
      {index > 0 ? <ContextMenuSeparator /> : null}
      {section}
    </React.Fragment>
  ))
}

/**
 * Wraps the grid's scroll area in one context menu. The clicked cell is read
 * from the event target, so rows don't each mount a menu.
 */
function DataTableContextMenu({
  children,
  items,
  render,
}: {
  children: React.ReactNode
  items?: DataTableContextMenuItems<RowData>
  render: React.ReactElement
}) {
  const [target, setTarget] = React.useState<Target | null>(null)
  return (
    <ContextMenu>
      <ContextMenuTrigger
        render={render}
        className="select-auto"
        onContextMenu={(event) => {
          const cell =
            event.target instanceof Element
              ? event.target.closest("td[data-row-id][data-column-id]")
              : null
          setTarget(
            cell
              ? {
                  rowId: cell.getAttribute("data-row-id") ?? "",
                  columnId: cell.getAttribute("data-column-id") ?? "",
                }
              : null
          )
        }}
      >
        {children}
      </ContextMenuTrigger>
      <ContextMenuContent className="w-56">
        <CellMenuItems target={target} items={items} />
      </ContextMenuContent>
    </ContextMenu>
  )
}

export { DataTableContextMenu }
