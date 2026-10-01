import type {
  DataTableFeatureConfig,
  InitialTableState,
} from "@/components/forge/data-table"

/** The admin tables: search, sort, columns and export, nothing heavier. */
export const ADMIN_TABLE_FEATURES: Partial<DataTableFeatureConfig> = {
  columnDragging: false,
  columnFilters: false,
  editing: false,
  expanding: false,
  grouping: false,
  rowPinning: false,
  rowSelection: false,
}

/** Keeps the row menu in view when a table scrolls sideways. */
export const PIN_ACTIONS: InitialTableState["columnPinning"] = {
  start: [],
  end: ["actions"],
}

export const DATE_TIME = { dateStyle: "medium", timeStyle: "short" } as const
