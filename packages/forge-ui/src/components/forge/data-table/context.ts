import * as React from "react"
import type { RowData } from "@tanstack/react-table"

import type { Row, Table } from "./table-config"
import type {
  DataTableDensity,
  DataTableFeatureConfig,
  DataTableLabels,
  DataTableRowReorder,
} from "./types"

export type EditingCell = {
  rowId: string
  columnId: string
  /** Text typed to start editing; replaces the value in the editor. */
  initial?: string
}

export type DataTableContextValue = {
  // The context is shared by generic subcomponents; row data is opaque here.
  table: Table<RowData>
  features: DataTableFeatureConfig
  labels: DataTableLabels
  /** The density in effect. */
  density: DataTableDensity
  /** The density picked for this table, or undefined when it follows the default. */
  densityChoice: DataTableDensity | undefined
  /** Pick a density for this table; "auto" follows the default again. */
  setDensity: (density: DataTableDensity | "auto") => void
  showFilterRow: boolean
  setShowFilterRow: (show: boolean) => void
  /** First data column; it carries tree indentation and toggles. */
  treeColumnId?: string
  treeData: boolean
  // Column drag-and-drop
  draggingColumnId: string | null
  setDraggingColumnId: (id: string | null) => void
  // Row drag-and-drop
  draggingRowId: string | null
  setDraggingRowId: (id: string | null) => void
  onRowReorder?: (event: DataTableRowReorder<RowData>) => void
  // Inline editing
  editing: EditingCell | null
  startEditing: (cell: EditingCell) => void
  stopEditing: () => void
  canEdit: (row: Row<RowData>, columnId: string) => boolean
  commitEdit: (row: Row<RowData>, columnId: string, value: unknown) => void
  // Actions
  exportCsv: (scope: "all" | "selected") => void
  copyRows: (rows: Row<RowData>[]) => void
  copySelection: () => void
  /** Shows a short confirmation in the status bar and announces it. */
  announce: (message: string) => void
}

export const DataTableContext =
  React.createContext<DataTableContextValue | null>(null)

export function useDataTable() {
  const context = React.useContext(DataTableContext)
  if (!context)
    throw new Error("DataTable parts must render inside <DataTable>.")
  return context
}
