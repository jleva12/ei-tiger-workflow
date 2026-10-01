import * as React from "react"
import { cn } from "cn"
import type { RowData } from "@tanstack/react-table"

import { Button } from "@/components/ui/button"
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import {
  InputGroup,
  InputGroupAddon,
  InputGroupInput,
} from "@/components/ui/input-group"
import { Icon } from "../icon"
import { CountBadge } from "../status"
import { ToolbarButton } from "../toolbar"
import { useDataTable } from "./context"
import { FilterPanel } from "./filters"
import type { Table } from "./table-config"
import type { DataTableDensity, DataTableSelectedRowsActions } from "./types"
import { getColumnLabel, isUtilityColumnId } from "./utils"

function resetDataTable(table: Table<RowData>) {
  table.resetSorting()
  table.resetColumnFilters()
  table.setGlobalFilter("")
  table.resetColumnVisibility()
  table.resetColumnOrder()
  table.resetColumnPinning()
  table.resetRowPinning()
  table.resetGrouping()
  table.resetExpanded()
  table.resetPagination()
  table.resetRowSelection()
  table.resetColumnSizing()
  table.resetCellSelection(true)
}

function ColumnsMenu() {
  const { table, labels } = useDataTable()
  const columns = table
    .getAllLeafColumns()
    .filter((column) => column.getCanHide())
  return (
    <DropdownMenu>
      <DropdownMenuTrigger render={<ToolbarButton icon="view" />}>
        {labels.columns}
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="max-h-80 w-56">
        {/* Base UI requires GroupLabel (DropdownMenuLabel) inside a Group. */}
        <DropdownMenuGroup>
          <DropdownMenuLabel>{labels.columns}</DropdownMenuLabel>
          {columns.map((column) => (
            <DropdownMenuCheckboxItem
              key={column.id}
              checked={column.getIsVisible()}
              closeOnClick={false}
              onCheckedChange={(checked) => column.toggleVisibility(!!checked)}
            >
              {getColumnLabel(column)}
            </DropdownMenuCheckboxItem>
          ))}
        </DropdownMenuGroup>
        <DropdownMenuSeparator />
        <DropdownMenuGroup>
          <DropdownMenuItem
            disabled={table.getIsAllColumnsVisible()}
            onClick={() => table.toggleAllColumnsVisible(true)}
          >
            <Icon icon="view" className="text-muted-foreground" />
            {labels.showAllColumns}
          </DropdownMenuItem>
        </DropdownMenuGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

function ViewMenu({ filterRowAvailable }: { filterRowAvailable: boolean }) {
  const {
    table,
    features,
    labels,
    density,
    densityChoice,
    setDensity,
    showFilterRow,
    setShowFilterRow,
  } = useDataTable()
  const groupable = features.grouping
    ? table
        .getAllLeafColumns()
        .filter(
          (column) => !isUtilityColumnId(column.id) && column.getCanGroup()
        )
    : []
  const canExpand = features.expanding && table.getCanSomeRowsExpand()
  return (
    <DropdownMenu>
      <DropdownMenuTrigger render={<ToolbarButton icon="settings" />}>
        {labels.view}
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-56">
        <DropdownMenuGroup>
          <DropdownMenuLabel>{labels.density}</DropdownMenuLabel>
          <DropdownMenuRadioGroup
            value={densityChoice ?? "auto"}
            onValueChange={(value) =>
              setDensity(value as DataTableDensity | "auto")
            }
          >
            <DropdownMenuRadioItem value="auto">
              {labels.densityAuto}
              <span className="ml-auto text-2xs text-muted-foreground">
                {densityChoice
                  ? null
                  : density === "compact"
                    ? labels.compact
                    : labels.comfortable}
              </span>
            </DropdownMenuRadioItem>
            <DropdownMenuRadioItem value="default">
              {labels.comfortable}
            </DropdownMenuRadioItem>
            <DropdownMenuRadioItem value="compact">
              {labels.compact}
            </DropdownMenuRadioItem>
          </DropdownMenuRadioGroup>
        </DropdownMenuGroup>
        <DropdownMenuSeparator />
        <DropdownMenuGroup>
          {filterRowAvailable ? (
            <DropdownMenuCheckboxItem
              checked={showFilterRow}
              onCheckedChange={(checked) => setShowFilterRow(!!checked)}
            >
              {labels.showFilterRow}
            </DropdownMenuCheckboxItem>
          ) : null}
          {groupable.length ? (
            <DropdownMenuSub>
              <DropdownMenuSubTrigger>
                <Icon icon="layers" className="text-muted-foreground" />
                {labels.grouping}
              </DropdownMenuSubTrigger>
              <DropdownMenuSubContent className="w-52">
                <DropdownMenuGroup>
                  {groupable.map((column) => (
                    <DropdownMenuCheckboxItem
                      key={column.id}
                      checked={column.getIsGrouped()}
                      closeOnClick={false}
                      onCheckedChange={() => column.toggleGrouping()}
                    >
                      {getColumnLabel(column)}
                    </DropdownMenuCheckboxItem>
                  ))}
                </DropdownMenuGroup>
              </DropdownMenuSubContent>
            </DropdownMenuSub>
          ) : null}
          {canExpand ? (
            <>
              <DropdownMenuItem
                onClick={() => table.toggleAllRowsExpanded(true)}
              >
                <Icon icon="down" className="text-muted-foreground" />
                {labels.expandAll}
              </DropdownMenuItem>
              <DropdownMenuItem
                onClick={() => table.toggleAllRowsExpanded(false)}
              >
                <Icon icon="up" className="text-muted-foreground" />
                {labels.collapseAll}
              </DropdownMenuItem>
            </>
          ) : null}
          {features.columnSizing ? (
            <DropdownMenuItem onClick={() => table.resetColumnSizing(true)}>
              <Icon icon="refresh" className="text-muted-foreground" />
              {labels.resetColumnWidths}
            </DropdownMenuItem>
          ) : null}
        </DropdownMenuGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

function ExportMenu() {
  const { table, features, labels, exportCsv, copyRows } = useDataTable()
  const selected = features.rowSelection
    ? table.getSelectedRowModel().flatRows
    : []
  return (
    <DropdownMenu>
      <DropdownMenuTrigger render={<ToolbarButton icon="download" />}>
        {labels.export}
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-56">
        <DropdownMenuGroup>
          <DropdownMenuItem onClick={() => exportCsv("all")}>
            <Icon icon="download" className="text-muted-foreground" />
            {labels.exportCsv}
          </DropdownMenuItem>
          <DropdownMenuItem
            disabled={!selected.length}
            onClick={() => exportCsv("selected")}
          >
            <Icon icon="download" className="text-muted-foreground" />
            {labels.exportSelectedCsv}
          </DropdownMenuItem>
        </DropdownMenuGroup>
        <DropdownMenuSeparator />
        <DropdownMenuGroup>
          <DropdownMenuItem
            onClick={() =>
              copyRows(selected.length ? selected : table.getRowModel().rows)
            }
          >
            <Icon icon="copy" className="text-muted-foreground" />
            {labels.copyRows}
          </DropdownMenuItem>
        </DropdownMenuGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

function renderSelectedRowsActions(
  actions: DataTableSelectedRowsActions<RowData> | undefined,
  table: Table<RowData>
) {
  if (!actions) return null
  const selectedRows = table.getFilteredSelectedRowModel().flatRows
  if (!selectedRows.length) return null
  const content =
    typeof actions === "function" ? actions({ selectedRows, table }) : actions
  return content ? { count: selectedRows.length, content } : null
}

export type DataTableToolbarProps = {
  title?: string
  description?: string
  toolbarActions?: React.ReactNode
  selectedRowsActions?: DataTableSelectedRowsActions<RowData>
  searchInputRef?: React.Ref<HTMLInputElement>
  columnFiltersPosition: "toolbar" | "header"
}

function DataTableToolbar({
  title,
  description,
  toolbarActions,
  selectedRowsActions,
  searchInputRef,
  columnFiltersPosition,
}: DataTableToolbarProps) {
  const { table, features, labels } = useDataTable()
  const hasActiveFilters =
    table.atoms.columnFilters.get().length > 0 ||
    Boolean(table.atoms.globalFilter.get())
  const selection = renderSelectedRowsActions(selectedRowsActions, table)

  if (
    !features.toolbar &&
    !title &&
    !description &&
    !toolbarActions &&
    !selection
  ) {
    return null
  }

  return (
    <div
      data-slot="data-table-toolbar"
      className="flex min-h-14 flex-col gap-2.5 border-b px-4 py-2.5 @2xl/data-table:flex-row @2xl/data-table:items-center @2xl/data-table:justify-between"
    >
      {title || description ? (
        <div className="min-w-0">
          {title ? <h2 className="text-sm font-[550]">{title}</h2> : null}
          {description ? (
            <p className="mt-0.5 text-2xs text-muted-foreground">
              {description}
            </p>
          ) : null}
        </div>
      ) : null}
      <div className="flex flex-wrap items-center gap-[7px] @2xl/data-table:ml-auto">
        {features.toolbar && features.globalFilter ? (
          <InputGroup className="h-8 w-56 max-w-full text-muted-foreground">
            <InputGroupAddon>
              <Icon icon="search" />
            </InputGroupAddon>
            <InputGroupInput
              ref={searchInputRef}
              value={table.atoms.globalFilter.get() ?? ""}
              onChange={(event) => table.setGlobalFilter(event.target.value)}
              placeholder={labels.searchPlaceholder}
              aria-label={labels.searchPlaceholder}
              className="text-xs md:text-xs"
            />
          </InputGroup>
        ) : null}
        {features.toolbar &&
        features.columnFilters &&
        columnFiltersPosition === "toolbar" ? (
          <FilterPanel />
        ) : null}
        {features.toolbar && features.columnVisibility ? <ColumnsMenu /> : null}
        {features.toolbar && features.viewOptions ? (
          <ViewMenu
            filterRowAvailable={
              features.columnFilters && columnFiltersPosition === "header"
            }
          />
        ) : null}
        {features.toolbar && features.export ? <ExportMenu /> : null}
        {features.toolbar && hasActiveFilters ? (
          <Button
            type="button"
            variant="ghost"
            size="sm"
            className="text-muted-foreground"
            onClick={() => {
              table.resetColumnFilters()
              table.setGlobalFilter("")
            }}
          >
            <Icon icon="close" data-icon="inline-start" />
            {labels.clearFilters}
          </Button>
        ) : null}
        {features.toolbar ? (
          <Button
            type="button"
            variant="ghost"
            size="sm"
            className="text-muted-foreground"
            onClick={() => resetDataTable(table)}
          >
            <Icon icon="refresh" data-icon="inline-start" />
            {labels.reset}
          </Button>
        ) : null}
        {selection ? (
          <div className="flex items-center gap-2 border-l pl-2.5">
            <CountBadge className="h-5 px-1.5 leading-[1.125rem]">
              {selection.count}
            </CountBadge>
            {selection.content}
          </div>
        ) : null}
        {toolbarActions ? (
          <div className="flex items-center gap-2">{toolbarActions}</div>
        ) : null}
      </div>
    </div>
  )
}

/**
 * Shows the grouped columns as removable chips and accepts column headers
 * dropped onto it, like a spreadsheet's row-group panel.
 */
function GroupingBar() {
  const { table, features, labels, draggingColumnId, setDraggingColumnId } =
    useDataTable()
  const [over, setOver] = React.useState(false)
  const grouping = table.atoms.grouping.get()
  const dragging = draggingColumnId
    ? table.getColumn(draggingColumnId)
    : undefined
  const canDrop = Boolean(dragging?.getCanGroup() && !dragging.getIsGrouped())

  if (!features.grouping || (!grouping.length && !canDrop)) return null

  return (
    <div
      data-slot="data-table-grouping"
      data-over={(over && canDrop) || undefined}
      className={cn(
        "flex min-h-10 flex-wrap items-center gap-1.5 border-b px-4 py-1.5 text-2xs text-muted-foreground transition-colors",
        canDrop &&
          "border-dashed bg-[color-mix(in_oklch,var(--muted)_35%,var(--background))]",
        "data-over:bg-[color-mix(in_oklch,var(--focus)_8%,var(--background))]"
      )}
      onDragOver={(event) => {
        if (!canDrop) return
        event.preventDefault()
        setOver(true)
      }}
      onDragLeave={() => setOver(false)}
      onDrop={(event) => {
        if (!canDrop || !dragging) return
        event.preventDefault()
        dragging.toggleGrouping()
        setOver(false)
        setDraggingColumnId(null)
      }}
    >
      <Icon icon="layers" size={14} className="text-subtle" />
      {grouping.length
        ? grouping.map((columnId, index) => {
            const column = table.getColumn(columnId)
            return (
              <React.Fragment key={columnId}>
                {index > 0 ? (
                  <Icon icon="right" size={12} className="text-subtle" />
                ) : null}
                <span className="inline-flex h-6 items-center gap-1 rounded-(--radius-chip) border bg-background pr-0.5 pl-2 text-xs text-foreground">
                  {column ? getColumnLabel(column) : columnId}
                  <Button
                    type="button"
                    variant="ghost"
                    size="icon-xs"
                    className="size-5 text-subtle"
                    aria-label={`Ungroup ${column ? getColumnLabel(column) : columnId}`}
                    onClick={() => column?.toggleGrouping()}
                  >
                    <Icon icon="close" size={12} />
                  </Button>
                </span>
              </React.Fragment>
            )
          })
        : null}
      {canDrop ? (
        <span className="text-subtle">
          Drop to group by {dragging ? getColumnLabel(dragging) : ""}
        </span>
      ) : grouping.length ? (
        <Button
          type="button"
          variant="ghost"
          size="xs"
          className="ml-auto text-muted-foreground"
          onClick={() => table.resetGrouping(true)}
        >
          {labels.reset}
        </Button>
      ) : null}
    </div>
  )
}

export { DataTableToolbar, GroupingBar }
