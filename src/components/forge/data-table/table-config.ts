// TanStack Table v9 feature registration and typed helpers for DataTable.
// Import column helpers and types from "./index" so they share these features.

import type * as React from "react"
import {
  aggregationFns,
  cellSelectionFeature,
  cellSpanningFeature,
  columnFacetingFeature,
  columnFilteringFeature,
  columnGroupingFeature,
  columnOrderingFeature,
  columnPinningFeature,
  columnResizingFeature,
  columnSizingFeature,
  columnVisibilityFeature,
  createColumnHelper as createTanStackColumnHelper,
  createExpandedRowModel,
  createFacetedMinMaxValues,
  createFacetedRowModel,
  createFacetedUniqueValues,
  createFilteredRowModel,
  createGroupedRowModel,
  createPaginatedRowModel,
  createSortedRowModel,
  filterFns,
  globalFilteringFeature,
  rowAggregationFeature,
  rowExpandingFeature,
  rowPaginationFeature,
  rowPinningFeature,
  rowSelectionFeature,
  rowSortingFeature,
  sortFns,
  tableFeatures,
  type Cell as CoreCell,
  type CellContext as CoreCellContext,
  type CellData,
  type Column as CoreColumn,
  type ColumnDef as CoreColumnDef,
  type Header as CoreHeader,
  type HeaderContext as CoreHeaderContext,
  type Row as CoreRow,
  type RowData,
  type Table as CoreTable,
  type TableFeatures,
  type TableOptions as CoreTableOptions,
  type TableState as CoreTableState,
} from "@tanstack/react-table"

// Register every capability DataTable exposes. Feature flags control their
// UI and client-side processing while keeping callbacks and column types stable.
export const dataTableFeatures = tableFeatures({
  cellSelectionFeature,
  cellSpanningFeature,
  columnFacetingFeature,
  columnFilteringFeature,
  columnGroupingFeature,
  columnOrderingFeature,
  columnPinningFeature,
  columnResizingFeature,
  columnSizingFeature,
  columnVisibilityFeature,
  globalFilteringFeature,
  rowAggregationFeature,
  rowExpandingFeature,
  rowPaginationFeature,
  rowPinningFeature,
  rowSelectionFeature,
  rowSortingFeature,
  expandedRowModel: createExpandedRowModel(),
  facetedMinMaxValues: createFacetedMinMaxValues(),
  facetedRowModel: createFacetedRowModel(),
  facetedUniqueValues: createFacetedUniqueValues(),
  filteredRowModel: createFilteredRowModel(),
  groupedRowModel: createGroupedRowModel(),
  paginatedRowModel: createPaginatedRowModel(),
  sortedRowModel: createSortedRowModel(),
  aggregationFns,
  filterFns,
  sortFns,
})

type Features = typeof dataTableFeatures

/** The filter control a column shows. Inferred from the data when omitted. */
export type DataTableFilterVariant =
  "text" | "range" | "date" | "select" | "multiSelect" | "boolean" | "none"

/** Built-in value formatters (Intl-based). */
export type DataTableFormat =
  | "number"
  | "integer"
  | "compact"
  | "currency"
  | "percent"
  | "date"
  | "datetime"
  | "time"

/** Inline editor used when `meta.editable` is set. Inferred when omitted. */
export type DataTableEditor = "text" | "number" | "select" | "date" | "checkbox"

/** Footer summary computed over the filtered rows. */
export type DataTableSummary =
  "sum" | "mean" | "median" | "min" | "max" | "count"

export type DataTableOption = {
  label: string
  value: string | number | boolean
}

declare module "@tanstack/react-table" {
  interface ColumnMeta<
    TFeatures extends TableFeatures,
    TData extends RowData,
    TValue extends CellData = CellData,
  > {
    align?: "left" | "center" | "right"
    className?: string
    headerClassName?: string
    cellClassName?:
      string | ((row: CoreRow<TFeatures, TData>, value: TValue) => string)
    filterPlaceholder?: string
    label?: string
    /** Filter control for this column; inferred from its values when omitted. */
    filterVariant?: DataTableFilterVariant
    /** Fixed options for select filters (otherwise faceted from the data). */
    filterOptions?: ReadonlyArray<DataTableOption>
    /** Formats values in cells, footers, exports and copies. */
    format?:
      | DataTableFormat
      | ((value: TValue, row: CoreRow<TFeatures, TData>) => React.ReactNode)
    /** Intl options for `format` (e.g. `{ currency: "EUR" }`). */
    formatOptions?: Intl.NumberFormatOptions & Intl.DateTimeFormatOptions
    /** Enables inline editing for every row, or per row. */
    editable?: boolean | ((row: CoreRow<TFeatures, TData>) => boolean)
    editor?: DataTableEditor
    editorOptions?: ReadonlyArray<DataTableOption>
    /** Returns an error message to keep the editor open, or nothing to accept. */
    validate?: (
      value: unknown,
      row: CoreRow<TFeatures, TData>
    ) => string | undefined | null
    /** Adds a footer summary for this column. */
    summary?: DataTableSummary
    /** Let long values wrap instead of truncating. */
    wrap?: boolean
  }
}

export type Column<TData extends RowData, TValue = unknown> = CoreColumn<
  Features,
  TData,
  TValue
>
export type ColumnDef<TData extends RowData, TValue = unknown> = CoreColumnDef<
  Features,
  TData,
  TValue
>
export type CellContext<
  TData extends RowData,
  TValue = unknown,
> = CoreCellContext<Features, TData, TValue>
export type Cell<TData extends RowData, TValue = unknown> = CoreCell<
  Features,
  TData,
  TValue
>
export type HeaderContext<
  TData extends RowData,
  TValue = unknown,
> = CoreHeaderContext<Features, TData, TValue>
export type Header<TData extends RowData, TValue = unknown> = CoreHeader<
  Features,
  TData,
  TValue
>
export type Row<TData extends RowData> = CoreRow<Features, TData>
export type Table<TData extends RowData> = CoreTable<Features, TData>
export type TableOptions<TData extends RowData> = CoreTableOptions<
  Features,
  TData
>
export type TableState = CoreTableState<Features>
export type InitialTableState = Partial<TableState>

export function createColumnHelper<TData extends RowData>() {
  return createTanStackColumnHelper<Features, TData>()
}
