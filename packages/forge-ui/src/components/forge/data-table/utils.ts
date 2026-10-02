import type { RowData } from "@tanstack/react-table"

import { localDay, parseTimestamp } from "@/lib/timestamps"
import type {
  Column,
  DataTableEditor,
  DataTableFilterVariant,
  DataTableOption,
  DataTableSummary,
  Row,
  Table,
} from "./table-config"

/** The parts of column meta the formatter reads. */
type FormatMeta =
  | {
      format?: unknown
      formatOptions?: Intl.NumberFormatOptions & Intl.DateTimeFormatOptions
    }
  | undefined

/** Utility columns (selection, pinning, drag…) are prefixed with "__". */
export function isUtilityColumnId(id: string) {
  return id.startsWith("__")
}

export function getColumnLabel<TData extends RowData>(
  column: Column<TData, unknown>
) {
  return column.columnDef.meta?.label ?? column.id
}

export function getAlignClass(align?: "left" | "center" | "right") {
  if (align === "center") return "text-center"
  if (align === "right") return "text-right"
  return "text-left"
}

/* -------------------------------------------------------------------------- */
/* Values and formatting                                                      */
/* -------------------------------------------------------------------------- */

export function toDate(value: unknown): Date | undefined {
  if (value instanceof Date)
    return Number.isNaN(value.getTime()) ? undefined : value
  if (typeof value === "number" || typeof value === "string") {
    const date = parseTimestamp(value)
    return Number.isNaN(date.getTime()) ? undefined : date
  }
  return undefined
}

const numberFormatters = new Map<string, Intl.NumberFormat>()
const dateFormatters = new Map<string, Intl.DateTimeFormat>()

function numberFormat(options: Intl.NumberFormatOptions) {
  const key = JSON.stringify(options)
  let formatter = numberFormatters.get(key)
  if (!formatter) {
    formatter = new Intl.NumberFormat(undefined, options)
    numberFormatters.set(key, formatter)
  }
  return formatter
}

function dateFormat(options: Intl.DateTimeFormatOptions) {
  const key = JSON.stringify(options)
  let formatter = dateFormatters.get(key)
  if (!formatter) {
    formatter = new Intl.DateTimeFormat(undefined, options)
    dateFormatters.set(key, formatter)
  }
  return formatter
}

/** Formats a value with a built-in formatter; returns plain text. */
export function formatValue(value: unknown, meta: FormatMeta): string {
  if (value === null || value === undefined || value === "") return ""
  const format = meta?.format
  const options = meta?.formatOptions ?? {}
  if (typeof format !== "string") {
    if (value instanceof Date)
      return dateFormat({ dateStyle: "medium" }).format(value)
    return String(value)
  }
  const number = typeof value === "number" ? value : Number(value)
  switch (format) {
    case "number":
      return Number.isFinite(number)
        ? numberFormat(options).format(number)
        : String(value)
    case "integer":
      return Number.isFinite(number)
        ? numberFormat({ maximumFractionDigits: 0, ...options }).format(number)
        : String(value)
    case "compact":
      return Number.isFinite(number)
        ? numberFormat({ notation: "compact", ...options }).format(number)
        : String(value)
    case "currency":
      return Number.isFinite(number)
        ? numberFormat({
            style: "currency",
            currency: "USD",
            ...options,
          }).format(number)
        : String(value)
    case "percent":
      return Number.isFinite(number)
        ? numberFormat({
            style: "percent",
            maximumFractionDigits: 1,
            ...options,
          }).format(number)
        : String(value)
    case "date":
    case "datetime":
    case "time": {
      const date = toDate(value)
      if (!date) return String(value)
      const defaults: Intl.DateTimeFormatOptions =
        format === "date"
          ? { dateStyle: "medium" }
          : format === "time"
            ? { timeStyle: "short" }
            : { dateStyle: "medium", timeStyle: "short" }
      return dateFormat(
        Object.keys(options).length ? options : defaults
      ).format(date)
    }
    default:
      return String(value)
  }
}

const dateFormats = new Set(["date", "datetime", "time"])

/** True for columns showing dates, including timestamps stored as numbers. */
export function isDateColumn<TData extends RowData>(
  column: Column<TData, unknown>
) {
  const meta = column.columnDef.meta
  return (
    meta?.filterVariant === "date" ||
    (typeof meta?.format === "string" && dateFormats.has(meta.format))
  )
}

/**
 * Text used for CSV export and clipboard copies: raw values, so numbers stay
 * numbers in a spreadsheet, and ISO 8601 for dates.
 */
export function getExportText<TData extends RowData>(
  row: Row<TData>,
  column: Column<TData, unknown>
) {
  const value = row.getValue(column.id)
  if (value === null || value === undefined) return ""
  if (value instanceof Date || isDateColumn(column)) {
    return toDate(value)?.toISOString() ?? String(value)
  }
  if (typeof value === "object") return JSON.stringify(value)
  return String(value)
}

/** Picks an editor from the current value when a column doesn't set one. */
export function inferEditor(
  value: unknown,
  hasOptions: boolean
): DataTableEditor {
  if (hasOptions) return "select"
  if (typeof value === "boolean") return "checkbox"
  if (typeof value === "number") return "number"
  if (value instanceof Date) return "date"
  return "text"
}

/**
 * Converts pasted or cleared text for a column's editor. Returns `undefined`
 * when the text can't become a valid value (the cell is left alone).
 */
export function coerceText(
  text: string,
  editor: DataTableEditor,
  previous: unknown,
  options?: ReadonlyArray<DataTableOption>
): unknown {
  const trimmed = text.trim()
  switch (editor) {
    case "number": {
      if (trimmed === "") return null
      const number = Number(trimmed.replace(/[,\s]/g, ""))
      return Number.isFinite(number) ? number : undefined
    }
    case "checkbox":
      if (/^(true|yes|y|1|x|✓)$/i.test(trimmed)) return true
      if (/^(false|no|n|0|)$/i.test(trimmed)) return false
      return undefined
    case "date": {
      if (trimmed === "") return null
      // A person's input: a time without a zone is their own, local one.
      const date = toDate(trimmed.includes(":") ? new Date(trimmed) : trimmed)
      if (!date) return undefined
      if (previous instanceof Date) return date
      if (typeof previous === "number") return date.getTime()
      // Kept as text: a day as the day, a time as UTC with its zone.
      return trimmed.includes(":") ? date.toISOString() : localDay(date)
    }
    case "select": {
      const option = options?.find(
        (item) => String(item.value) === trimmed || item.label === trimmed
      )
      return option ? option.value : undefined
    }
    default:
      return text
  }
}

/** Picks a filter control from a sample value when a column doesn't set one. */
export function inferFilterVariant(value: unknown): DataTableFilterVariant {
  if (typeof value === "number") return "range"
  if (typeof value === "boolean") return "boolean"
  if (value instanceof Date) return "date"
  return "text"
}

export const filterFnForVariant: Record<
  Exclude<DataTableFilterVariant, "none">,
  string
> = {
  text: "includesString",
  range: "inNumberRange",
  date: "inDateRange",
  select: "equals",
  multiSelect: "arrHas",
  boolean: "equals",
}

/** Reads a column's value from a raw data object (for type inference). */
export function sampleColumnValue(
  column: { accessorKey?: unknown; accessorFn?: unknown },
  sample: unknown
): unknown {
  if (sample === undefined || sample === null) return undefined
  if (typeof column.accessorFn === "function") {
    try {
      return (column.accessorFn as (row: unknown, index: number) => unknown)(
        sample,
        0
      )
    } catch {
      return undefined
    }
  }
  if (typeof column.accessorKey === "string") {
    return column.accessorKey
      .split(".")
      .reduce<unknown>(
        (value, key) =>
          value && typeof value === "object"
            ? (value as Record<string, unknown>)[key]
            : undefined,
        sample
      )
  }
  return undefined
}

/* -------------------------------------------------------------------------- */
/* Summaries                                                                  */
/* -------------------------------------------------------------------------- */

export const summaryLabels: Record<DataTableSummary, string> = {
  sum: "Sum",
  mean: "Avg",
  median: "Median",
  min: "Min",
  max: "Max",
  count: "Count",
}

export function summarize(values: unknown[], kind: DataTableSummary) {
  if (kind === "count") return values.filter((value) => value != null).length
  const numbers = values
    .map((value) => (value instanceof Date ? value.getTime() : Number(value)))
    .filter((value) => Number.isFinite(value))
  if (!numbers.length) return undefined
  switch (kind) {
    case "sum":
      return numbers.reduce((total, value) => total + value, 0)
    case "mean":
      return numbers.reduce((total, value) => total + value, 0) / numbers.length
    case "min":
      return Math.min(...numbers)
    case "max":
      return Math.max(...numbers)
    case "median": {
      const sorted = [...numbers].sort((a, b) => a - b)
      const middle = Math.floor(sorted.length / 2)
      return sorted.length % 2
        ? sorted[middle]
        : (sorted[middle - 1] + sorted[middle]) / 2
    }
  }
}

/** Count / Sum / Avg / Min / Max of numeric values, as shown in the status bar. */
export function selectionStats(values: unknown[]) {
  const numbers = values.filter(
    (value): value is number =>
      typeof value === "number" && Number.isFinite(value)
  )
  const count = values.filter(
    (value) => value !== null && value !== undefined && value !== ""
  ).length
  if (!numbers.length) return { count, numeric: 0 }
  const sum = numbers.reduce((total, value) => total + value, 0)
  return {
    count,
    numeric: numbers.length,
    sum,
    mean: sum / numbers.length,
    min: Math.min(...numbers),
    max: Math.max(...numbers),
  }
}

/* -------------------------------------------------------------------------- */
/* Export and clipboard                                                       */
/* -------------------------------------------------------------------------- */

function escapeCsv(text: string) {
  return /[",\n\r]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text
}

/** Visible data columns in display order (utility columns excluded). */
export function getExportColumns<TData extends RowData>(table: Table<TData>) {
  return table
    .getVisibleLeafColumns()
    .filter((column) => !isUtilityColumnId(column.id))
}

export function rowsToCsv<TData extends RowData>(
  table: Table<TData>,
  rows: Row<TData>[]
) {
  const columns = getExportColumns(table)
  const header = columns.map((column) => escapeCsv(getColumnLabel(column)))
  const lines = rows.map((row) =>
    columns.map((column) => escapeCsv(getExportText(row, column))).join(",")
  )
  return [header.join(","), ...lines].join("\r\n")
}

export function rowsToTsv<TData extends RowData>(
  table: Table<TData>,
  rows: Row<TData>[],
  withHeader = true
) {
  const columns = getExportColumns(table)
  const clean = (text: string) => text.replace(/[\t\r\n]+/g, " ")
  const lines = rows.map((row) =>
    columns.map((column) => clean(getExportText(row, column))).join("\t")
  )
  return (
    withHeader
      ? [
          columns.map((column) => clean(getColumnLabel(column))).join("\t"),
          ...lines,
        ]
      : lines
  ).join("\n")
}

/**
 * Serializes the selected cells as tab-separated text, row by row in display
 * order. Disjoint ranges share one grid with blanks between them.
 */
export function selectionToTsv<TData extends RowData>(table: Table<TData>) {
  const columnIds = new Set(table.getCellSelectionColumnIds())
  const columns = table
    .getVisibleLeafColumns()
    .filter((column) => columnIds.has(column.id))
  const rows = table
    .getCellSelectionRowIds()
    .map((id) => table.getRow(id, true))
    .filter((row): row is Row<TData> => Boolean(row))
    .sort((a, b) => a.getDisplayIndex() - b.getDisplayIndex())
  const clean = (text: string) => text.replace(/[\t\r\n]+/g, " ")
  return rows
    .map((row) => {
      const cells = row.getAllCellsByColumnId()
      return columns
        .map((column) =>
          cells[column.id]?.getIsSelected()
            ? clean(getExportText(row, column))
            : ""
        )
        .join("\t")
    })
    .join("\n")
}

export function downloadText(content: string, fileName: string, type: string) {
  const url = URL.createObjectURL(new Blob([content], { type }))
  const link = document.createElement("a")
  link.href = url
  link.download = fileName
  link.click()
  setTimeout(() => URL.revokeObjectURL(url), 0)
}

export async function copyText(text: string) {
  try {
    await navigator.clipboard.writeText(text)
    return true
  } catch {
    return false
  }
}

/* -------------------------------------------------------------------------- */
/* Persistence                                                                */
/* -------------------------------------------------------------------------- */

const storagePrefix = "forge-data-table:"

export function readStoredState<T>(key: string | undefined): Partial<T> {
  if (!key || typeof window === "undefined") return {}
  try {
    const raw = window.localStorage.getItem(storagePrefix + key)
    return raw ? (JSON.parse(raw) as Partial<T>) : {}
  } catch {
    return {}
  }
}

export function writeStoredState(key: string | undefined, value: unknown) {
  if (!key || typeof window === "undefined") return
  try {
    window.localStorage.setItem(storagePrefix + key, JSON.stringify(value))
  } catch {
    // Storage can be unavailable (private mode, quota); persistence is optional.
  }
}
