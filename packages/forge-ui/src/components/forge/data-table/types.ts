import type * as React from "react"
import type { RowData } from "@tanstack/react-table"

import type { Row, Table } from "./table-config"

export type DataTableFeatureConfig = {
  /** The toolbar row (search, menus, actions). */
  toolbar: boolean
  /** Quick filter across every column. */
  globalFilter: boolean
  /** Typed per-column filters (header row or toolbar panel). */
  columnFilters: boolean
  columnVisibility: boolean
  /** Move columns from the column menu. */
  columnOrdering: boolean
  /** Drag column headers to reorder them. */
  columnDragging: boolean
  columnPinning: boolean
  /** Drag header edges to resize; double-click to reset. */
  columnSizing: boolean
  sorting: boolean
  /** Shift-click headers to sort by several columns. */
  multiSort: boolean
  /** Group rows by a column with aggregation. */
  grouping: boolean
  /** Expandable detail rows and tree data (`getSubRows`). */
  expanding: boolean
  pagination: boolean
  rowSelection: boolean
  rowPinning: boolean
  /** Unique values and min/max for filter options and placeholders. */
  faceting: boolean
  /** Footer row (column `footer` or `meta.summary`). */
  footers: boolean
  /** Spreadsheet-style range selection, keyboard navigation and copy. */
  cellSelection: boolean
  /** Honor `spanRows` / `spanColumns` in column definitions. */
  cellSpanning: boolean
  /** Inline editing for columns with `meta.editable`. */
  editing: boolean
  /** Render only visible rows (for 1,000+ rows). Disables pagination. */
  virtualization: boolean
  /** CSV export and copy actions. */
  export: boolean
  /** Right-click menu on cells. */
  contextMenu: boolean
  /** A leading row-number column. */
  rowNumbers: boolean
  /** Drag rows to reorder (requires `onRowReorder`). */
  rowReordering: boolean
  /** Row counts and selected-cell statistics under the grid. */
  statusBar: boolean
  /** The View menu (density, filter row, expand all, reset widths). */
  viewOptions: boolean
}

export type DataTableLabels = {
  searchPlaceholder: string
  columns: string
  tableOptions: string
  reset: string
  noResults: string
  loading: string
  rowsSelected: string
  rows: string
  rowSingular?: string
  page: string
  of: string
  firstPage: string
  previousPage: string
  nextPage: string
  lastPage: string
  rowsPerPage: string
  filter: string
  grouping: string
  pinLeft: string
  pinRight: string
  unpin: string
  moveLeft: string
  moveRight: string
  pinTop: string
  pinBottom: string
  unpinRow: string
  rowOptions: string
  expand: string
  collapse: string
  selectAll: string
  selectRow: string
  filters: string
  clearFilters: string
  view: string
  density: string
  densityAuto: string
  comfortable: string
  compact: string
  showFilterRow: string
  expandAll: string
  collapseAll: string
  resetColumnWidths: string
  showAllColumns: string
  export: string
  exportCsv: string
  exportSelectedCsv: string
  copyRows: string
  copyCell: string
  copyRow: string
  copySelection: string
  sortAscending: string
  sortDescending: string
  clearSort: string
  hideColumn: string
  resetWidth: string
  all: string
  yes: string
  no: string
  min: string
  max: string
  from: string
  to: string
  searchValues: string
  selectedValues: string
  noValues: string
  rowNumber: string
  dragRow: string
  deselectRow: string
  showing: string
}

/** Row height: 45px like the task list, or 34px for dense data. */
export type DataTableDensity = "default" | "compact"

export type DataTableCellEdit<TData extends RowData> = {
  row: Row<TData>
  columnId: string
  value: unknown
  previousValue: unknown
}

export type DataTableRowReorder<TData extends RowData> = {
  row: Row<TData>
  targetRow: Row<TData>
  position: "before" | "after"
}

export type DataTableSelectedRowsActionsContext<TData extends RowData> = {
  selectedRows: Row<TData>[]
  table: Table<TData>
}

export type DataTableSelectedRowsActions<TData extends RowData> =
  | React.ReactNode
  | ((context: DataTableSelectedRowsActionsContext<TData>) => React.ReactNode)

export const defaultFeatures: DataTableFeatureConfig = {
  toolbar: true,
  globalFilter: true,
  columnFilters: true,
  columnVisibility: true,
  columnOrdering: true,
  columnDragging: true,
  columnPinning: true,
  columnSizing: true,
  sorting: true,
  multiSort: true,
  grouping: true,
  expanding: true,
  pagination: true,
  rowSelection: true,
  rowPinning: true,
  faceting: true,
  footers: false,
  cellSelection: false,
  cellSpanning: true,
  editing: true,
  virtualization: false,
  export: true,
  contextMenu: true,
  rowNumbers: false,
  rowReordering: false,
  statusBar: true,
  viewOptions: true,
}

export const defaultLabels: DataTableLabels = {
  searchPlaceholder: "Search rows...",
  columns: "Columns",
  tableOptions: "Table options",
  reset: "Reset",
  noResults: "No results found.",
  loading: "Loading rows...",
  rowsSelected: "selected",
  rows: "rows",
  page: "Page",
  of: "of",
  firstPage: "First page",
  previousPage: "Previous page",
  nextPage: "Next page",
  lastPage: "Last page",
  rowsPerPage: "Rows per page",
  filter: "Filter",
  grouping: "Grouping",
  pinLeft: "Pin left",
  pinRight: "Pin right",
  unpin: "Unpin",
  moveLeft: "Move left",
  moveRight: "Move right",
  pinTop: "Pin top",
  pinBottom: "Pin bottom",
  unpinRow: "Unpin row",
  rowOptions: "Row options",
  expand: "Expand row",
  collapse: "Collapse row",
  selectAll: "Select all visible rows",
  selectRow: "Select row",
  filters: "Filters",
  clearFilters: "Clear filters",
  view: "View",
  density: "Density",
  densityAuto: "Automatic",
  comfortable: "Comfortable",
  compact: "Compact",
  showFilterRow: "Show filter row",
  expandAll: "Expand all",
  collapseAll: "Collapse all",
  resetColumnWidths: "Reset column widths",
  showAllColumns: "Show all columns",
  export: "Export",
  exportCsv: "Export CSV",
  exportSelectedCsv: "Export selected rows",
  copyRows: "Copy rows",
  copyCell: "Copy cell",
  copyRow: "Copy row",
  copySelection: "Copy selection",
  sortAscending: "Sort ascending",
  sortDescending: "Sort descending",
  clearSort: "Clear sort",
  hideColumn: "Hide column",
  resetWidth: "Reset width",
  all: "All",
  yes: "Yes",
  no: "No",
  min: "Min",
  max: "Max",
  from: "From",
  to: "To",
  searchValues: "Search values…",
  selectedValues: "selected",
  noValues: "No values",
  rowNumber: "#",
  dragRow: "Drag to reorder",
  deselectRow: "Deselect row",
  showing: "Showing",
}
