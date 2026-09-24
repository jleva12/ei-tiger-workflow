import * as React from "react"
import { cn } from "cn"
import {
  flexRender,
  useTable,
  type CellSelectionDirection,
  type ColumnFiltersState,
  type ColumnOrderState,
  type ColumnPinningState,
  type ColumnSizingState,
  type ColumnVisibilityState,
  type ExpandedState,
  type GroupingState,
  type OnChangeFn,
  type PaginationState,
  type RowData,
  type RowPinningState,
  type RowSelectionState,
  type SortingState,
  type Updater,
} from "@tanstack/react-table"

import { Spinner } from "@/components/ui/spinner"
import {
  TableCaption,
  TableCell,
  TableFooter,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { DataTableBody } from "./body"
import { DefaultCell, DefaultFooter, DefaultHeader } from "./cells"
import {
  DataTableContext,
  type DataTableContextValue,
  type EditingCell,
} from "./context"
import {
  DataTableContextMenu,
  type DataTableContextMenuItems,
} from "./context-menu"
import { ColumnFilter } from "./filters"
import { HeaderCell } from "./header"
import {
  getFlexibleColumn,
  getPinnedClass,
  getPinnedStyles,
  rowHeightPx,
  subtleSurface,
} from "./layout"
import { DataTableStatusBar } from "./status-bar"
import {
  dataTableFeatures,
  type Column,
  type ColumnDef,
  type InitialTableState,
  type Row,
  type Table,
  type TableOptions,
  type TableState,
} from "./table-config"
import { usePreferredDensity } from "./density"
import { DataTableToolbar, GroupingBar } from "./toolbar"
import {
  defaultFeatures,
  defaultLabels,
  type DataTableCellEdit,
  type DataTableDensity,
  type DataTableFeatureConfig,
  type DataTableLabels,
  type DataTableRowReorder,
  type DataTableSelectedRowsActions,
} from "./types"
import {
  coerceText,
  copyText,
  downloadText,
  filterFnForVariant,
  getAlignClass,
  inferEditor,
  inferFilterVariant,
  isUtilityColumnId,
  readStoredState,
  rowsToCsv,
  rowsToTsv,
  sampleColumnValue,
  selectionToTsv,
  writeStoredState,
} from "./utils"
import {
  createExpanderColumn,
  createPinningColumn,
  createRowDragColumn,
  createRowNumberColumn,
  createSelectionColumn,
} from "./utility-columns"

type DataTableOptions<TData extends RowData> = Partial<
  Omit<TableOptions<TData>, "columns" | "data" | "features" | "state">
> & {
  state?: Partial<TableState>
}

type DataTableProps<TData extends RowData> = {
  columns: TableOptions<TData>["columns"]
  data: TData[]
  caption?: string
  className?: string
  /** Where column filters live: a row under the header, or a toolbar panel. */
  columnFiltersPosition?: "toolbar" | "header"
  /** Right-click menu entries added after the built-in ones. */
  contextMenuItems?: DataTableContextMenuItems<TData>
  /**
   * Row density. Default: the user's density preference
   * (`UserPreferencesProvider`), otherwise comfortable. People can pick a
   * density for this table from the View menu, or go back to Automatic.
   */
  density?: DataTableDensity
  description?: string
  emptyState?: React.ReactNode
  /** File name (without extension) for CSV exports. */
  exportFileName?: string
  features?: Partial<DataTableFeatureConfig>
  getRowId?: TableOptions<TData>["getRowId"]
  /** Children of a row, for tree data. */
  getSubRows?: TableOptions<TData>["getSubRows"]
  initialState?: InitialTableState
  isLoading?: boolean
  labels?: Partial<DataTableLabels>
  /** Called when an inline edit, paste or clear commits a new value. */
  onCellEdit?: (edit: DataTableCellEdit<TData>) => void
  onDensityChange?: (density: DataTableDensity) => void
  onRowClick?: (row: Row<TData>) => void
  onRowDoubleClick?: (row: Row<TData>) => void
  /** Called when a row is dropped next to another (enables drag handles). */
  onRowReorder?: (event: DataTableRowReorder<TData>) => void
  pinningColumn?: boolean
  renderSubComponent?: (row: Row<TData>) => React.ReactNode
  rowClassName?: string | ((row: Row<TData>) => string)
  searchInputRef?: React.Ref<HTMLInputElement>
  selectedRowsActions?: DataTableSelectedRowsActions<TData>
  selectionColumn?: boolean
  skeletonRows?: number
  /** Persists column layout, sorting and density in localStorage under this key. */
  stateKey?: string
  tableOptions?: DataTableOptions<TData>
  /** A class limiting the scroll area's height, e.g. `max-h-[32rem]`. */
  tableHeight?: string
  /** @deprecated Use tableHeight. */
  tablehight?: string
  title?: string
  toolbarActions?: React.ReactNode
}

type StoredState = {
  columnVisibility: ColumnVisibilityState
  columnOrder: ColumnOrderState
  columnSizing: ColumnSizingState
  columnPinning: ColumnPinningState
  sorting: SortingState
  /** Only a density picked in the View menu is kept. */
  densityChoice: DataTableDensity | undefined
  showFilterRow: boolean
}

function updateState<T>(updater: Updater<T>, previous: T) {
  return typeof updater === "function"
    ? (updater as (previous: T) => T)(previous)
    : updater
}

function useForwardedState<T>(
  initialState: T,
  onChange?: OnChangeFn<T>
): [T, OnChangeFn<T>] {
  const [state, setState] = React.useState(initialState)
  const setForwardedState = React.useCallback<OnChangeFn<T>>(
    (updater) => {
      setState((previous) => updateState(updater, previous))
      onChange?.(updater)
    },
    [onChange]
  )
  return [state, setForwardedState]
}

function useElementHeight(ref: React.RefObject<HTMLElement | null>) {
  const [height, setHeight] = React.useState(0)
  React.useLayoutEffect(() => {
    const element = ref.current
    if (!element) return
    const observer = new ResizeObserver(() => setHeight(element.offsetHeight))
    observer.observe(element)
    setHeight(element.offsetHeight)
    return () => observer.disconnect()
  }, [ref])
  return height
}

type LooseColumnDef = ColumnDef<RowData, unknown> & {
  accessorKey?: unknown
  accessorFn?: unknown
  columns?: LooseColumnDef[]
  filterFn?: unknown
  spanRows?: unknown
  spanColumns?: unknown
}

/**
 * Fills in what a column can infer from the data: its filter control and
 * filter function, and right alignment for numbers.
 */
function enhanceColumns<TData extends RowData>(
  columns: TableOptions<TData>["columns"],
  sample: TData | undefined
): TableOptions<TData>["columns"] {
  const enhance = (def: LooseColumnDef): LooseColumnDef => {
    if (def.columns?.length)
      return { ...def, columns: def.columns.map(enhance) }
    const meta = def.meta
    const value = sampleColumnValue(def, sample)
    const filterVariant =
      meta?.filterVariant ??
      (meta?.filterOptions ? "select" : inferFilterVariant(value))
    const next = {
      ...def,
      meta: {
        ...meta,
        filterVariant,
        align: meta?.align ?? (typeof value === "number" ? "right" : undefined),
      },
    } as LooseColumnDef
    if (filterVariant === "none") next.enableColumnFilter = false
    else if (!def.filterFn) {
      next.filterFn = filterFnForVariant[
        filterVariant
      ] as LooseColumnDef["filterFn"]
    }
    return next
  }
  return (columns as unknown as LooseColumnDef[]).map(
    enhance
  ) as unknown as TableOptions<TData>["columns"]
}

function hasSpanDefs(columns: ReadonlyArray<unknown>): boolean {
  return (columns as LooseColumnDef[]).some(
    (def) =>
      Boolean(def.spanRows || def.spanColumns) ||
      (def.columns ? hasSpanDefs(def.columns) : false)
  )
}

// Lets spreadsheet apps detect UTF-8 in exported CSV files.
const byteOrderMark = "\uFEFF"

const arrowDirections: Record<string, CellSelectionDirection> = {
  ArrowUp: "up",
  ArrowDown: "down",
  ArrowLeft: "left",
  ArrowRight: "right",
}

/**
 * A data grid on TanStack Table v9 in the workspace style. Beyond the table
 * basics (sorting, filtering, grouping, pinning, pagination) it covers the
 * spreadsheet-grade parts: typed filters, range selection with copy and
 * paste, inline editing, virtualization, tree data, cell spanning, column
 * groups, drag to reorder and group, CSV export, a context menu, a status
 * bar and persisted layouts.
 */
function DataTable<TData extends RowData>({
  caption,
  className,
  columnFiltersPosition = "header",
  columns,
  contextMenuItems,
  data,
  density: densityProp,
  description,
  emptyState,
  exportFileName = "export",
  features: featureOverrides,
  getRowId,
  getSubRows,
  initialState,
  isLoading = false,
  labels: labelOverrides,
  onCellEdit,
  onDensityChange,
  onRowClick,
  onRowDoubleClick,
  onRowReorder,
  pinningColumn,
  renderSubComponent,
  rowClassName,
  searchInputRef,
  selectedRowsActions,
  selectionColumn,
  skeletonRows = 5,
  stateKey,
  tableOptions,
  tableHeight,
  tablehight,
  title,
  toolbarActions,
}: DataTableProps<TData>) {
  const features = React.useMemo<DataTableFeatureConfig>(
    () => ({ ...defaultFeatures, ...featureOverrides }),
    [featureOverrides]
  )
  const labels = React.useMemo<DataTableLabels>(
    () => ({ ...defaultLabels, ...labelOverrides }),
    [labelOverrides]
  )
  const [stored] = React.useState(() => readStoredState<StoredState>(stateKey))
  const treeData = Boolean(getSubRows ?? tableOptions?.getSubRows)
  const paginated = features.pagination && !features.virtualization

  /* State ------------------------------------------------------------------ */

  const [sorting, setSorting] = useForwardedState<SortingState>(
    stored.sorting ?? initialState?.sorting ?? [],
    tableOptions?.onSortingChange
  )
  const [columnFilters, setColumnFilters] =
    useForwardedState<ColumnFiltersState>(
      initialState?.columnFilters ?? [],
      tableOptions?.onColumnFiltersChange
    )
  const [globalFilter, setGlobalFilter] = useForwardedState(
    initialState?.globalFilter ?? "",
    tableOptions?.onGlobalFilterChange
  )
  const [columnVisibility, setColumnVisibility] =
    useForwardedState<ColumnVisibilityState>(
      stored.columnVisibility ?? initialState?.columnVisibility ?? {},
      tableOptions?.onColumnVisibilityChange
    )
  const [columnOrder, setColumnOrder] = useForwardedState<ColumnOrderState>(
    stored.columnOrder ?? initialState?.columnOrder ?? [],
    tableOptions?.onColumnOrderChange
  )
  const [columnPinning, setColumnPinning] =
    useForwardedState<ColumnPinningState>(
      stored.columnPinning ??
        initialState?.columnPinning ?? { start: [], end: [] },
      tableOptions?.onColumnPinningChange
    )
  const [columnSizing, setColumnSizing] = useForwardedState<ColumnSizingState>(
    stored.columnSizing ?? initialState?.columnSizing ?? {},
    tableOptions?.onColumnSizingChange
  )
  const [rowPinning, setRowPinning] = useForwardedState<RowPinningState>(
    initialState?.rowPinning ?? { top: [], bottom: [] },
    tableOptions?.onRowPinningChange
  )
  const [grouping, setGrouping] = useForwardedState<GroupingState>(
    initialState?.grouping ?? [],
    tableOptions?.onGroupingChange
  )
  const [expanded, setExpanded] = useForwardedState<ExpandedState>(
    initialState?.expanded ?? {},
    tableOptions?.onExpandedChange
  )
  const [pagination, setPagination] = useForwardedState<PaginationState>(
    {
      pageIndex: initialState?.pagination?.pageIndex ?? 0,
      pageSize: initialState?.pagination?.pageSize ?? 10,
    },
    tableOptions?.onPaginationChange
  )
  const [rowSelection, setRowSelection] = useForwardedState<RowSelectionState>(
    initialState?.rowSelection ?? {},
    tableOptions?.onRowSelectionChange
  )

  // A density picked in the View menu wins; otherwise the `density` prop,
  // otherwise the user's density preference.
  const preferredDensity = usePreferredDensity()
  const [densityChoice, setDensityChoice] = React.useState<
    DataTableDensity | undefined
  >(stored.densityChoice)
  const [previousDensityProp, setPreviousDensityProp] =
    React.useState(densityProp)
  if (densityProp !== previousDensityProp) {
    setPreviousDensityProp(densityProp)
    setDensityChoice(undefined)
  }
  const density = densityChoice ?? densityProp ?? preferredDensity
  const setDensity = React.useCallback(
    (next: DataTableDensity | "auto") => {
      const choice = next === "auto" ? undefined : next
      setDensityChoice(choice)
      onDensityChange?.(choice ?? densityProp ?? preferredDensity)
    },
    [onDensityChange, densityProp, preferredDensity]
  )
  const [showFilterRow, setShowFilterRow] = React.useState(
    stored.showFilterRow ?? true
  )
  const [draggingColumnId, setDraggingColumnId] = React.useState<string | null>(
    null
  )
  const [draggingRowId, setDraggingRowId] = React.useState<string | null>(null)
  const [editing, setEditing] = React.useState<EditingCell | null>(null)
  const [notice, setNotice] = React.useState<string | null>(null)
  const noticeTimer = React.useRef<number | undefined>(undefined)
  React.useEffect(() => () => window.clearTimeout(noticeTimer.current), [])

  React.useEffect(() => {
    writeStoredState(stateKey, {
      columnVisibility,
      columnOrder,
      columnSizing,
      columnPinning,
      sorting,
      densityChoice,
      showFilterRow,
    } satisfies StoredState)
  }, [
    stateKey,
    columnVisibility,
    columnOrder,
    columnSizing,
    columnPinning,
    sorting,
    densityChoice,
    showFilterRow,
  ])

  /* Columns ---------------------------------------------------------------- */

  const sample = data[0]
  const enhancedColumns = React.useMemo(
    () => enhanceColumns(columns, sample),
    [columns, sample]
  )
  const hasSpans = React.useMemo(() => hasSpanDefs(columns), [columns])

  const { resolvedColumns, utilityIds } = React.useMemo(() => {
    const utility: ColumnDef<TData, unknown>[] = []
    if (features.rowReordering && onRowReorder) {
      utility.push(createRowDragColumn<TData>())
    }
    if (features.rowSelection && (selectionColumn ?? true)) {
      utility.push(createSelectionColumn<TData>(labels))
    }
    if (features.rowNumbers) utility.push(createRowNumberColumn<TData>(labels))
    if (features.expanding && renderSubComponent && !treeData) {
      utility.push(createExpanderColumn<TData>(labels))
    }
    if (features.rowPinning && (pinningColumn ?? true)) {
      utility.push(createPinningColumn<TData>(labels))
    }
    return {
      resolvedColumns: [...utility, ...enhancedColumns],
      utilityIds: utility.map((column) => column.id as string),
    }
  }, [
    enhancedColumns,
    features.expanding,
    features.rowNumbers,
    features.rowPinning,
    features.rowReordering,
    features.rowSelection,
    labels,
    onRowReorder,
    pinningColumn,
    renderSubComponent,
    selectionColumn,
    treeData,
  ])

  // Utility columns lead the order, and pin with the first pinned column
  // so a pinned column never floats in front of the checkboxes.
  const effectiveOrder = React.useMemo(
    () =>
      columnOrder.length
        ? [...utilityIds, ...columnOrder.filter((id) => !isUtilityColumnId(id))]
        : columnOrder,
    [columnOrder, utilityIds]
  )
  const effectivePinning = React.useMemo<ColumnPinningState>(() => {
    const start = (columnPinning.start ?? []).filter(
      (id) => !isUtilityColumnId(id)
    )
    return {
      start: start.length ? [...utilityIds, ...start] : [],
      end: columnPinning.end ?? [],
    }
  }, [columnPinning, utilityIds])

  /* Table ------------------------------------------------------------------ */

  const table = useTable({
    ...tableOptions,
    features: dataTableFeatures,
    defaultColumn: {
      header: DefaultHeader,
      cell: DefaultCell,
      footer: DefaultFooter,
      ...tableOptions?.defaultColumn,
    } as TableOptions<TData>["defaultColumn"],
    columns: resolvedColumns,
    data,
    getRowId: getRowId ?? tableOptions?.getRowId,
    getSubRows: getSubRows ?? tableOptions?.getSubRows,
    initialState,
    state: {
      sorting,
      columnFilters,
      globalFilter,
      columnVisibility,
      columnOrder: effectiveOrder,
      columnPinning: effectivePinning,
      columnSizing,
      rowPinning,
      grouping,
      expanded,
      pagination,
      rowSelection,
      ...tableOptions?.state,
    },
    // Edits replace `data`; keep the page, expansion and selection.
    autoResetCellSelection: tableOptions?.autoResetCellSelection ?? false,
    autoResetExpanded: tableOptions?.autoResetExpanded ?? false,
    autoResetPageIndex: tableOptions?.autoResetPageIndex ?? false,
    columnResizeMode: tableOptions?.columnResizeMode ?? "onChange",
    enableCellSelection:
      features.cellSelection && (tableOptions?.enableCellSelection ?? true),
    enableCellSpanning:
      features.cellSpanning && hasSpans && !features.virtualization,
    enableColumnFilters: features.columnFilters,
    enableColumnPinning: features.columnPinning,
    enableColumnResizing: features.columnSizing,
    enableExpanding: features.expanding,
    enableGlobalFilter: features.globalFilter,
    enableGrouping: features.grouping,
    enableHiding: features.columnVisibility,
    enableMultiRowSelection:
      features.rowSelection && (tableOptions?.enableMultiRowSelection ?? true),
    enableMultiSort:
      features.multiSort && (tableOptions?.enableMultiSort ?? true),
    enableRowPinning: features.rowPinning,
    enableRowSelection:
      features.rowSelection && (tableOptions?.enableRowSelection ?? true),
    enableSorting: features.sorting,
    // Tree data keeps parents of matching children.
    filterFromLeafRows: tableOptions?.filterFromLeafRows ?? treeData,
    getRowCanExpand:
      tableOptions?.getRowCanExpand ??
      (renderSubComponent && !treeData ? () => true : undefined),
    manualExpanding: tableOptions?.manualExpanding ?? !features.expanding,
    manualFiltering:
      tableOptions?.manualFiltering ??
      !(features.globalFilter || features.columnFilters),
    manualGrouping: tableOptions?.manualGrouping ?? !features.grouping,
    manualPagination: tableOptions?.manualPagination ?? !paginated,
    manualSorting: tableOptions?.manualSorting ?? !features.sorting,
    onColumnFiltersChange: setColumnFilters,
    onColumnOrderChange: setColumnOrder,
    onColumnPinningChange: setColumnPinning,
    onColumnSizingChange: setColumnSizing,
    onColumnVisibilityChange: setColumnVisibility,
    onExpandedChange: setExpanded,
    onGlobalFilterChange: setGlobalFilter,
    onGroupingChange: setGrouping,
    onPaginationChange: setPagination,
    onRowPinningChange: setRowPinning,
    onRowSelectionChange: setRowSelection,
    onSortingChange: setSorting,
  })
  // Subcomponents share one context and treat row data as opaque.
  const gridTable = table as unknown as Table<RowData>

  /* Actions ---------------------------------------------------------------- */

  const announce = (message: string) => {
    setNotice(message)
    window.clearTimeout(noticeTimer.current)
    noticeTimer.current = window.setTimeout(() => setNotice(null), 2200)
  }

  const canEdit = (row: Row<RowData>, columnId: string) => {
    if (!features.editing || !onCellEdit) return false
    if (isUtilityColumnId(columnId) || row.getIsGrouped()) return false
    const editable = gridTable.getColumn(columnId)?.columnDef.meta?.editable
    return typeof editable === "function" ? editable(row) : Boolean(editable)
  }

  const commitEdit = (row: Row<RowData>, columnId: string, value: unknown) => {
    const previousValue = row.getValue(columnId)
    if (Object.is(previousValue, value)) return
    onCellEdit?.({
      row: row as unknown as Row<TData>,
      columnId,
      value,
      previousValue,
    })
  }

  const exportCsv = (scope: "all" | "selected") => {
    const rows =
      scope === "selected"
        ? gridTable.getSelectedRowModel().flatRows
        : gridTable
            .getSortedRowModel()
            .flatRows.filter((row) => !row.getIsGrouped())
    downloadText(
      `${byteOrderMark}${rowsToCsv(gridTable, rows)}`,
      `${exportFileName}.csv`,
      "text/csv;charset=utf-8"
    )
    announce(`Exported ${rows.length} ${rows.length === 1 ? "row" : "rows"}`)
  }

  const copyRows = async (rows: Row<RowData>[]) => {
    if (await copyText(rowsToTsv(gridTable, rows))) {
      announce(`Copied ${rows.length} ${rows.length === 1 ? "row" : "rows"}`)
    }
  }

  const copySelection = async () => {
    const count = gridTable.getSelectedCellCount()
    if (!count) return
    if (await copyText(selectionToTsv(gridTable))) {
      announce(`Copied ${count} ${count === 1 ? "cell" : "cells"}`)
    }
  }

  const startEditing = (cell: EditingCell) => {
    if (!features.cellSelection) return setEditing(cell)
    gridTable.setFocusedCell(cell.rowId, cell.columnId)
    setEditing(cell)
  }

  const contextValue: DataTableContextValue = {
    table: gridTable,
    features,
    labels,
    density,
    densityChoice,
    setDensity,
    showFilterRow,
    setShowFilterRow,
    treeColumnId: treeData
      ? gridTable
          .getVisibleLeafColumns()
          .find((column) => !isUtilityColumnId(column.id))?.id
      : undefined,
    treeData,
    draggingColumnId,
    setDraggingColumnId,
    draggingRowId,
    setDraggingRowId,
    onRowReorder: onRowReorder as DataTableContextValue["onRowReorder"],
    editing,
    startEditing,
    stopEditing: () => setEditing(null),
    canEdit,
    commitEdit,
    exportCsv,
    copyRows,
    copySelection,
    announce,
  }

  /* Keyboard and clipboard ------------------------------------------------- */

  const scrollRef = React.useRef<HTMLDivElement>(null)
  const headerRef = React.useRef<HTMLTableSectionElement>(null)
  const footerRef = React.useRef<HTMLTableSectionElement>(null)
  const headerHeight = useElementHeight(headerRef)
  const footerHeight = useElementHeight(footerRef)

  const selectableColumns = () =>
    gridTable
      .getVisibleLeafColumns()
      .filter(
        (column) =>
          !isUtilityColumnId(column.id) &&
          column.columnDef.enableCellSelection !== false
      )

  const forEachSelectedCell = (
    visit: (row: Row<RowData>, column: Column<RowData, unknown>) => void
  ) => {
    const columnIds = gridTable.getCellSelectionColumnIds()
    for (const rowId of gridTable.getCellSelectionRowIds()) {
      const row = gridTable.getRow(rowId, true)
      if (!row) continue
      const cells = row.getAllCellsByColumnId()
      for (const columnId of columnIds) {
        const cell = cells[columnId]
        if (cell?.getIsSelected()) visit(row, cell.column)
      }
    }
  }

  const applyText = (
    row: Row<RowData>,
    column: Column<RowData, unknown>,
    text: string
  ) => {
    if (!canEdit(row, column.id)) return false
    const meta = column.columnDef.meta
    const previous = row.getValue(column.id)
    const editor =
      meta?.editor ?? inferEditor(previous, Boolean(meta?.editorOptions))
    const value = coerceText(text, editor, previous, meta?.editorOptions)
    if (value === undefined || meta?.validate?.(value, row)) return false
    commitEdit(row, column.id, value)
    return true
  }

  // Moves focus (optionally extending the active range) to a cell.
  const moveTo = (rowId: string, columnId: string, extend: boolean) => {
    const active = gridTable.atoms.cellSelection.get().at(-1)
    if (extend && active) {
      gridTable.selectCellRange({
        anchorRowId: active.anchorRowId,
        anchorColumnId: active.anchorColumnId,
        focusRowId: rowId,
        focusColumnId: columnId,
      })
    } else {
      gridTable.setFocusedCell(rowId, columnId)
    }
  }

  const onGridKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    if (!features.cellSelection || editing) return
    const target = event.target as HTMLElement
    // Controls inside cells (checkboxes, buttons, menus) keep their keys.
    if (target !== event.currentTarget && target.tagName !== "TD") return
    const rows = gridTable.getRowModel().rows
    const columns = selectableColumns()
    const focused = gridTable.getFocusedCell()
    const mod = event.metaKey || event.ctrlKey
    const key = event.key

    if (!focused) {
      if (key in arrowDirections || key === "Home" || key === "End") {
        event.preventDefault()
        if (rows[0] && columns[0]) {
          gridTable.setFocusedCell(rows[0].id, columns[0].id)
        }
      }
      return
    }

    const active = gridTable.atoms.cellSelection.get().at(-1)
    const focusRowId = active?.focusRowId ?? focused.row.id
    const focusColumnId = active?.focusColumnId ?? focused.column.id

    const direction = arrowDirections[key]
    if (direction) {
      event.preventDefault()
      if (mod) {
        // Jump to the edge of the grid.
        const edgeRow =
          direction === "up"
            ? rows[0]
            : direction === "down"
              ? rows.at(-1)
              : undefined
        const edgeColumn =
          direction === "left"
            ? columns[0]
            : direction === "right"
              ? columns.at(-1)
              : undefined
        moveTo(
          edgeRow?.id ?? focusRowId,
          edgeColumn?.id ?? focusColumnId,
          event.shiftKey
        )
      } else if (event.shiftKey) {
        gridTable.extendCellSelection(direction)
      } else {
        gridTable.moveCellSelection(direction)
      }
      return
    }

    switch (key) {
      case "Home":
      case "End": {
        event.preventDefault()
        const column = key === "Home" ? columns[0] : columns.at(-1)
        const row = mod ? (key === "Home" ? rows[0] : rows.at(-1)) : undefined
        if (column) moveTo(row?.id ?? focusRowId, column.id, event.shiftKey)
        return
      }
      case "PageUp":
      case "PageDown": {
        event.preventDefault()
        const step = Math.max(
          1,
          Math.floor(
            (scrollRef.current?.clientHeight ?? 400) / rowHeightPx[density]
          ) - 2
        )
        const index = rows.findIndex((row) => row.id === focusRowId)
        const next =
          rows[
            Math.min(
              rows.length - 1,
              Math.max(0, index + (key === "PageDown" ? step : -step))
            )
          ]
        if (next) moveTo(next.id, focusColumnId, event.shiftKey)
        return
      }
      case "Enter":
      case "F2": {
        event.preventDefault()
        if (canEdit(focused.row, focused.column.id)) {
          startEditing({ rowId: focused.row.id, columnId: focused.column.id })
        } else if (focused.row.getCanExpand()) {
          focused.row.toggleExpanded()
        }
        return
      }
      case " ": {
        if (features.rowSelection && focused.row.getCanSelect()) {
          event.preventDefault()
          focused.row.toggleSelected()
        }
        return
      }
      case "Escape": {
        if (gridTable.getSelectedCellCount() > 1) {
          event.preventDefault()
          gridTable.setFocusedCell(focused.row.id, focused.column.id)
        }
        return
      }
      case "Delete":
      case "Backspace": {
        if (!features.editing || !onCellEdit) return
        event.preventDefault()
        let cleared = 0
        forEachSelectedCell((row, column) => {
          if (applyText(row, column, "")) cleared += 1
        })
        if (cleared > 1) announce(`Cleared ${cleared} cells`)
        return
      }
    }

    if (mod && key.toLowerCase() === "a") {
      event.preventDefault()
      gridTable.selectAllCells()
      return
    }
    if (mod && key.toLowerCase() === "c") {
      event.preventDefault()
      void copySelection()
      return
    }
    // Typing on a cell starts editing it with that character.
    if (
      !mod &&
      !event.altKey &&
      key.length === 1 &&
      canEdit(focused.row, focused.column.id)
    ) {
      const meta = focused.column.columnDef.meta
      const editor =
        meta?.editor ??
        inferEditor(focused.getValue(), Boolean(meta?.editorOptions))
      if (editor === "checkbox") return
      event.preventDefault()
      startEditing({
        rowId: focused.row.id,
        columnId: focused.column.id,
        initial: editor === "select" ? undefined : key,
      })
    }
  }

  const onGridPaste = (event: React.ClipboardEvent<HTMLDivElement>) => {
    if (
      !features.cellSelection ||
      !features.editing ||
      !onCellEdit ||
      editing
    ) {
      return
    }
    const focused = gridTable.getFocusedCell()
    const text = event.clipboardData.getData("text/plain")
    if (!focused || !text) return
    event.preventDefault()
    const grid = text
      .replace(/\r\n?/g, "\n")
      .replace(/\n$/, "")
      .split("\n")
      .map((line) => line.split("\t"))
    let pasted = 0
    if (
      grid.length === 1 &&
      grid[0].length === 1 &&
      gridTable.getSelectedCellCount() > 1
    ) {
      // One value fills the whole selection.
      forEachSelectedCell((row, column) => {
        if (applyText(row, column, grid[0][0])) pasted += 1
      })
    } else {
      const rows = gridTable.getRowModel().rows
      const columns = selectableColumns()
      const firstRow = rows.findIndex((row) => row.id === focused.row.id)
      const firstColumn = columns.findIndex(
        (column) => column.id === focused.column.id
      )
      grid.forEach((values, rowOffset) => {
        const row = rows[firstRow + rowOffset]
        values.forEach((value, columnOffset) => {
          const column = columns[firstColumn + columnOffset]
          if (row && column && applyText(row, column, value)) pasted += 1
        })
      })
    }
    if (pasted) announce(`Pasted ${pasted} ${pasted === 1 ? "cell" : "cells"}`)
  }

  /* Render ----------------------------------------------------------------- */

  const headerGroups = gridTable.getHeaderGroups()
  const footerGroup = gridTable.getFooterGroups()[0]
  const leafColumns = [
    ...gridTable.getStartVisibleLeafColumns(),
    ...gridTable.getCenterVisibleLeafColumns(),
    ...gridTable.getEndVisibleLeafColumns(),
  ]
  const flexibleColumn = getFlexibleColumn(gridTable)
  const hasRows = gridTable.getRowModel().rows.length > 0
  const focusedCell = features.cellSelection
    ? gridTable.getFocusedCell()
    : undefined

  const tableElement = (
    <table
      data-slot="table"
      role={features.cellSelection ? "grid" : undefined}
      aria-multiselectable={features.cellSelection || undefined}
      className="w-full caption-bottom border-collapse text-xs tabular-nums"
      style={{
        minWidth: gridTable.getTotalSize(),
        width: flexibleColumn ? "100%" : gridTable.getTotalSize(),
        tableLayout: "fixed",
      }}
    >
      {caption ? (
        <TableCaption className="mt-0 border-t px-4 py-2.5 text-left text-2xs text-muted-foreground">
          {caption}
        </TableCaption>
      ) : null}
      {/* Fixed layout reads widths from here, so column groups and spans
          in the first header row can't redistribute them. */}
      <colgroup>
        {leafColumns.map((column) => (
          <col
            key={column.id}
            style={{
              width:
                column.id === flexibleColumn?.id ? undefined : column.getSize(),
            }}
          />
        ))}
      </colgroup>
      <TableHeader
        ref={headerRef}
        className="sticky top-0 z-20 bg-background shadow-[0_1px_0_var(--border)] [&_tr]:border-0"
      >
        {headerGroups.map((headerGroup, index) => (
          <TableRow
            key={headerGroup.id}
            className="border-b hover:bg-transparent has-aria-expanded:bg-transparent"
          >
            {headerGroup.headers.map((header) => (
              <HeaderCell
                key={header.id}
                header={header}
                isGroupRow={index < headerGroups.length - 1}
              />
            ))}
          </TableRow>
        ))}
        {features.columnFilters &&
        columnFiltersPosition === "header" &&
        showFilterRow ? (
          <TableRow
            className={cn(
              "border-b hover:bg-transparent has-aria-expanded:bg-transparent",
              subtleSurface
            )}
          >
            {leafColumns.map((column) => (
              <TableHead
                key={column.id}
                className={cn(
                  "h-auto px-[17px] py-2 align-middle font-normal",
                  isUtilityColumnId(column.id) && "px-0",
                  getPinnedClass(column, "filter"),
                  column.columnDef.meta?.headerClassName
                )}
                style={getPinnedStyles(column)}
              >
                <ColumnFilter column={column} />
              </TableHead>
            ))}
          </TableRow>
        ) : null}
      </TableHeader>
      <DataTableBody
        scrollRef={scrollRef}
        emptyState={emptyState}
        isLoading={isLoading}
        skeletonRows={skeletonRows}
        onRowClick={onRowClick as DataTableRowHandler}
        onRowDoubleClick={onRowDoubleClick as DataTableRowHandler}
        renderSubComponent={
          renderSubComponent as
            ((row: Row<RowData>) => React.ReactNode) | undefined
        }
        rowClassName={
          rowClassName as string | ((row: Row<RowData>) => string) | undefined
        }
      />
      {features.footers && footerGroup ? (
        <TableFooter
          ref={footerRef}
          className={cn(
            "sticky bottom-0 z-20 border-t font-medium shadow-[0_-1px_0_var(--border)]",
            subtleSurface
          )}
        >
          <TableRow className="hover:bg-transparent">
            {footerGroup.headers.map((header) => (
              <TableCell
                key={header.id}
                colSpan={header.colSpan}
                className={cn(
                  "truncate px-[17px] py-3 text-2xs text-muted-foreground",
                  getAlignClass(header.column.columnDef.meta?.align),
                  getPinnedClass(header.column, "filter")
                )}
                style={getPinnedStyles(header.column)}
              >
                {header.isPlaceholder
                  ? null
                  : flexRender(
                      header.column.columnDef.footer,
                      header.getContext()
                    )}
              </TableCell>
            ))}
          </TableRow>
        </TableFooter>
      ) : null}
    </table>
  )

  const scrollProps: React.ComponentProps<"div"> & { "data-slot": string } = {
    "data-slot": "table-container",
    // Before any cell has focus, the grid itself takes Tab and hands
    // focus to the first cell.
    tabIndex: features.cellSelection && !focusedCell ? 0 : undefined,
    className: cn(
      "relative w-full overflow-auto",
      tableHeight ??
        tablehight ??
        (features.virtualization ? "max-h-[600px]" : undefined)
    ),
    // Keep focused cells clear of the sticky header, footer and pinned columns.
    style: {
      scrollPaddingTop: headerHeight,
      scrollPaddingBottom: footerHeight,
      scrollPaddingInlineStart: gridTable.getStartTotalSize(),
      scrollPaddingInlineEnd: gridTable.getEndTotalSize(),
    },
    onFocus: (event) => {
      if (event.target !== event.currentTarget || focusedCell) return
      const row = gridTable.getRowModel().rows[0]
      const column = selectableColumns()[0]
      if (row && column) gridTable.setFocusedCell(row.id, column.id)
    },
    onKeyDown: onGridKeyDown,
    onPaste: onGridPaste,
  }

  return (
    <DataTableContext.Provider value={contextValue}>
      <section
        data-slot="data-table"
        data-density={density}
        aria-busy={isLoading}
        aria-label={title ?? caption}
        className={cn(
          "group/data-table @container/data-table relative min-w-0 overflow-hidden rounded-(--radius-band) border bg-background text-foreground",
          className
        )}
      >
        {isLoading ? (
          <span className="sr-only" role="status">
            {labels.loading}
          </span>
        ) : null}
        <DataTableToolbar
          title={title}
          description={description}
          toolbarActions={toolbarActions}
          selectedRowsActions={
            selectedRowsActions as DataTableSelectedRowsActions<RowData>
          }
          searchInputRef={searchInputRef}
          columnFiltersPosition={columnFiltersPosition}
        />
        <GroupingBar />
        <div className="relative">
          {features.contextMenu ? (
            <DataTableContextMenu
              items={
                contextMenuItems as unknown as
                  DataTableContextMenuItems<RowData> | undefined
              }
              render={<div ref={scrollRef} {...scrollProps} />}
            >
              {tableElement}
            </DataTableContextMenu>
          ) : (
            <div ref={scrollRef} {...scrollProps}>
              {tableElement}
            </div>
          )}
          {isLoading && hasRows ? (
            <div className="pointer-events-none absolute inset-x-0 top-16 z-30 flex justify-center">
              <span className="inline-flex items-center gap-2 rounded-full border bg-background px-3 py-1.5 text-2xs text-muted-foreground shadow-(--shadow-float)">
                <Spinner className="size-3.5" />
                {labels.loading}
              </span>
            </div>
          ) : null}
        </div>
        {features.statusBar || paginated ? (
          <DataTableStatusBar notice={notice} />
        ) : (
          <span className="sr-only" role="status" aria-live="polite">
            {notice}
          </span>
        )}
      </section>
    </DataTableContext.Provider>
  )
}

type DataTableRowHandler = ((row: Row<RowData>) => void) | undefined

export { DataTable, type DataTableOptions, type DataTableProps }
