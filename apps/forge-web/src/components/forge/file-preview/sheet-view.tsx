import * as React from "react"
import { cn } from "cn"
import { read, utils, type CellObject, type WorkBook } from "xlsx"

import {
  createColumnHelper,
  DataTable,
  type DataTableFeatureConfig,
  type InitialTableState,
} from "@/components/forge/data-table"
import { Switch } from "@/components/ui/switch"
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { readText } from "./content"
import {
  BarDivider,
  BarText,
  DownloadButton,
  PreviewFrame,
  PreviewLoading,
  PreviewProblem,
} from "./frame"
import { readFailure, usePreviewTask, type ViewProps } from "./preview"
import { extensionOf } from "./kinds"

// Past these, a sheet shows its first rows and columns and says so.
const MAX_ROWS = 100_000
const MAX_COLUMNS = 400

type Value = string | number | boolean | Date
type Cell = { value: Value; text: string; error?: boolean }

type Sheet = {
  name: string
  hidden: boolean
  /** Sheet rows, as they are (not counting the cut). */
  rows: (Cell | undefined)[][]
  columns: number
  /** Widths from the file, in px, where it has them. */
  widths: (number | undefined)[]
  /** Rows and columns left out past the limits. */
  cut: { rows: number; columns: number }
  /** Where the sheet starts: its first row and column, 0-based. */
  origin: { row: number; column: number }
}

/**
 * A workbook's sheets (Excel, OpenDocument, CSV, TSV) in the data table:
 * letters over the columns and the sheet's own row numbers down the side,
 * values as the file formats them (dates, currency, percentages), sorting,
 * search, range selection with copy and a sum of what's selected, and CSV
 * export of a sheet. The first row becomes the column names when it looks
 * like them; a switch changes that. Tabs pick the sheet.
 */
export default function SheetView({
  data,
  name,
  toolbarEnd,
  onDownload,
}: ViewProps) {
  const book = usePreviewTask(() => readBook(data, name), [data, name])
  const [active, setActive] = React.useState(0)
  const sheets = book.value ?? []
  const sheet = sheets[Math.min(active, Math.max(sheets.length - 1, 0))]
  const [headerChoice, setHeaderChoice] = React.useState<
    Record<number, boolean>
  >({})
  const header = sheet
    ? (headerChoice[active] ?? looksLikeHeader(sheet))
    : false

  if (book.error)
    return (
      <PreviewFrame toolbarEnd={toolbarEnd} desk>
        <PreviewProblem
          kind="sheet"
          tone="danger"
          title="Couldn't show this spreadsheet"
          description={readFailure("sheet", book.error)}
          actions={<DownloadButton onDownload={onDownload} />}
        />
      </PreviewFrame>
    )
  if (!book.value)
    return (
      <PreviewFrame toolbarEnd={toolbarEnd} desk>
        <PreviewLoading kind="sheet" label="Reading the sheets…" />
      </PreviewFrame>
    )

  return (
    <PreviewFrame
      toolbarEnd={toolbarEnd}
      className="flex flex-col"
      toolbar={
        <>
          {sheets.length > 1 && (
            <>
              <Tabs
                value={String(active)}
                onValueChange={(value) => setActive(Number(value))}
                className="min-w-0 shrink"
              >
                <TabsList
                  variant="line"
                  aria-label="Sheets"
                  className="h-10! gap-3 p-0"
                >
                  {sheets.map((each, index) => (
                    <TabsTrigger
                      key={index}
                      value={String(index)}
                      className={cn(
                        "h-10 flex-none px-0.5 text-xs",
                        each.hidden && "text-subtle italic"
                      )}
                      title={each.hidden ? `${each.name} (hidden)` : each.name}
                    >
                      {each.name}
                    </TabsTrigger>
                  ))}
                </TabsList>
              </Tabs>
              <BarDivider />
            </>
          )}
          {sheet && sheet.rows.length > 1 && (
            <label className="flex shrink-0 items-center gap-2 px-1.5 text-xs text-muted-foreground">
              <Switch
                checked={header}
                onCheckedChange={(checked) =>
                  setHeaderChoice((choices) => ({
                    ...choices,
                    [active]: checked,
                  }))
                }
              />
              First row is a header
            </label>
          )}
          {sheet && (sheet.cut.rows > 0 || sheet.cut.columns > 0) && (
            <>
              <BarDivider />
              <BarText className="text-danger-foreground">
                Showing the first {formatCount(sheet.rows.length)} rows
                {sheet.cut.columns > 0 &&
                  ` and ${formatCount(sheet.columns)} columns`}
              </BarText>
            </>
          )}
        </>
      }
    >
      {sheet && sheet.rows.length > 0 ? (
        <SheetTable
          // Another sheet, or the header row changing, starts a new table.
          key={`${active}:${header}`}
          sheet={sheet}
          header={header}
          fileName={exportName(
            name,
            sheets.length > 1 ? sheet.name : undefined
          )}
        />
      ) : (
        <PreviewProblem kind="sheet" title="This sheet is empty" />
      )}
    </PreviewFrame>
  )
}

type Row = { id: string; number: number; cells: (Cell | undefined)[] }

const FEATURES: Partial<DataTableFeatureConfig> = {
  toolbar: true,
  globalFilter: true,
  columnFilters: false,
  columnVisibility: true,
  columnOrdering: false,
  columnDragging: false,
  columnPinning: true,
  columnSizing: true,
  sorting: true,
  multiSort: true,
  grouping: false,
  expanding: false,
  pagination: false,
  rowSelection: false,
  rowPinning: false,
  faceting: false,
  footers: false,
  cellSelection: true,
  cellSpanning: false,
  editing: false,
  virtualization: true,
  export: true,
  contextMenu: true,
  rowNumbers: false,
  rowReordering: false,
  statusBar: true,
  viewOptions: true,
}

const helper = createColumnHelper<Row>()

function SheetTable({
  sheet,
  header,
  fileName,
}: {
  sheet: Sheet
  header: boolean
  fileName: string
}) {
  const names = header ? sheet.rows[0] : undefined
  const body = React.useMemo(
    () => (header ? sheet.rows.slice(1) : sheet.rows),
    [sheet, header]
  )
  const firstRow = sheet.origin.row + (header ? 2 : 1)

  // Widths from the file, or fitted to what's in the column (its name with
  // the header's sort and menu marks, and its first rows); set for every
  // column, so none stretches to fill the table as a record list's would.
  const sizes = React.useMemo(() => {
    const measure = textMeasure()
    return Array.from({ length: sheet.columns }, (_, index) => {
      const fromFile = sheet.widths[index]
      if (fromFile) return fromFile
      const widest = Math.max(
        measure(names?.[index]?.text ?? "") + 56,
        ...body
          .slice(0, 200)
          .map((cells) => measure(cells[index]?.text ?? "") + 32)
      )
      return Math.ceil(Math.min(Math.max(widest, 72), 360))
    })
  }, [sheet, names, body])
  const initialState = React.useMemo<InitialTableState>(
    () => ({
      columnPinning: { start: ["row"], end: [] },
      columnSizing: Object.fromEntries(
        sizes.map((size, index) => [`c${index}`, size])
      ),
    }),
    [sizes]
  )

  const rows = React.useMemo<Row[]>(
    () =>
      body.map((cells, index) => ({
        id: String(index),
        number: firstRow + index,
        cells,
      })),
    [body, firstRow]
  )

  const columns = React.useMemo(() => {
    const letters = Array.from({ length: sheet.columns }, (_, index) =>
      utils.encode_col(sheet.origin.column + index)
    )
    return helper.columns([
      helper.accessor("number", {
        id: "row",
        header: "",
        size: Math.max(48, String(firstRow + body.length).length * 7 + 30),
        enableHiding: false,
        enableResizing: false,
        enableGlobalFilter: false,
        meta: {
          label: "Row",
          align: "right",
          cellClassName: "text-2xs text-subtle tabular-nums",
        },
      }),
      ...letters.map((letter, index) => {
        const title = names?.[index]?.text.trim()
        const numeric = body
          .slice(0, 50)
          .some((cells) => typeof cells[index]?.value === "number")
        return helper.accessor((row) => row.cells[index]?.value, {
          id: `c${index}`,
          header: title || letter,
          size: sizes[index],
          minSize: 48,
          sortUndefined: "last",
          meta: {
            label: title ? `${letter} · ${title}` : letter,
            align: numeric ? "right" : undefined,
          },
          cell: ({ row }) => {
            const cell = row.original.cells[index]
            if (!cell) return null
            return (
              <span
                className={cn(
                  "whitespace-pre",
                  cell.error && "text-danger-foreground",
                  typeof cell.value === "number" && "tabular-nums"
                )}
                title={cell.text.length > 40 ? cell.text : undefined}
              >
                {cell.text}
              </span>
            )
          },
        })
      }),
    ])
  }, [sheet, names, body, firstRow, sizes])

  return (
    <DataTable
      columns={columns}
      data={rows}
      getRowId={(row) => row.id}
      features={FEATURES}
      initialState={initialState}
      density="compact"
      fill
      // All the height the viewer gives it (virtual rows cap it otherwise).
      tableHeight="max-h-none"
      className="flex-1 rounded-none border-0"
      description={`${formatCount(body.length)} ${body.length === 1 ? "row" : "rows"} · ${formatCount(sheet.columns)} ${sheet.columns === 1 ? "column" : "columns"}`}
      exportFileName={fileName}
      labels={{ searchPlaceholder: "Search this sheet…" }}
    />
  )
}

/**
 * Measures text as the grid's cells draw it (13px in the page's face), for
 * fitting columns to their contents; only the first line of a cell counts.
 */
function textMeasure() {
  const context = document.createElement("canvas").getContext("2d")
  if (!context) return (text: string) => text.length * 7.5
  context.font = `13px ${getComputedStyle(document.body).fontFamily}`
  return (text: string) =>
    text ? context.measureText(text.split("\n", 1)[0]).width : 0
}

/* -------------------------------------------------------------------------- */
/* Reading                                                                    */
/* -------------------------------------------------------------------------- */

async function readBook(data: Blob, name: string): Promise<Sheet[]> {
  const extension = extensionOf(name)
  let book: WorkBook
  if (extension === "csv" || extension === "tsv") {
    const text = await readText(data)
    if (!text) throw new Error("It isn't a text file.")
    // Values as written: "00123" stays, dates aren't guessed.
    book = read(text.text, {
      type: "string",
      raw: true,
      dense: true,
      ...(extension === "tsv" && { FS: "\t" }),
    })
  } else {
    book = read(await data.arrayBuffer(), {
      type: "array",
      dense: true,
      cellDates: true,
      cellNF: false,
      cellStyles: true,
    })
  }
  const text = extension === "csv" || extension === "tsv"
  return book.SheetNames.map((sheetName, index) =>
    toSheet(
      sheetName,
      book.Sheets[sheetName],
      Boolean(book.Workbook?.Sheets?.[index]?.Hidden),
      text
    )
  )
}

type DenseSheet = WorkBook["Sheets"][string] & {
  "!data"?: (CellObject | undefined)[][]
}

/**
 * @param text Read from CSV or TSV, where every value is text as written:
 *   plain numbers then sort and align as numbers, still shown as written.
 */
function toSheet(
  name: string,
  sheet: DenseSheet,
  hidden: boolean,
  text: boolean
): Sheet {
  const ref = sheet["!ref"]
  if (!ref)
    return {
      name,
      hidden,
      rows: [],
      columns: 0,
      widths: [],
      cut: { rows: 0, columns: 0 },
      origin: { row: 0, column: 0 },
    }
  const range = utils.decode_range(ref)
  const totalRows = range.e.r - range.s.r + 1
  const totalColumns = range.e.c - range.s.c + 1
  const rowCount = Math.min(totalRows, MAX_ROWS)
  const columnCount = Math.min(totalColumns, MAX_COLUMNS)
  const dense = sheet["!data"] ?? []
  const rows: (Cell | undefined)[][] = []
  for (let r = 0; r < rowCount; r++) {
    const source = dense[range.s.r + r] ?? []
    const cells: (Cell | undefined)[] = []
    for (let c = 0; c < columnCount; c++)
      cells.push(toCell(source[range.s.c + c], text))
    rows.push(cells)
  }
  // Trailing empty rows (a range stretched by formatting) aren't shown.
  while (rows.length > 0 && rows[rows.length - 1].every((cell) => !cell))
    rows.pop()
  const widths = Array.from({ length: columnCount }, (_, c) => {
    const column = sheet["!cols"]?.[range.s.c + c]
    // Excel's widths count characters of its default font; the cells here
    // add their padding and the header its sort and filter marks.
    const px = column?.wch ? column.wch * 7.5 : column?.wpx
    return px ? Math.min(Math.max(px + 28, 64), 480) : undefined
  })
  return {
    name,
    hidden,
    rows,
    columns: columnCount,
    widths,
    cut: { rows: totalRows - rowCount, columns: totalColumns - columnCount },
    origin: { row: range.s.r, column: range.s.c },
  }
}

// A plain number, not an ID: "00123" and 16+ digits stay text.
const PLAIN_NUMBER = /^[-+]?(?:0|[1-9]\d{0,14})(?:\.\d+)?$/

function toCell(
  cell: CellObject | undefined,
  fromText: boolean
): Cell | undefined {
  if (!cell || cell.t === "z" || cell.v === undefined || cell.v === null)
    return undefined
  const value = cell.v as Value
  if (value === "") return undefined
  if (fromText && typeof value === "string")
    return {
      value: PLAIN_NUMBER.test(value.trim()) ? Number(value) : value,
      text: value,
    }
  let text: string
  try {
    text = cell.w ?? utils.format_cell(cell)
  } catch {
    text = String(value)
  }
  if (value instanceof Date && !cell.w) text = value.toLocaleDateString()
  return { value, text, error: cell.t === "e" }
}

/**
 * Whether a sheet's first row names its columns: distinct text over most of
 * the columns, above a row with numbers, dates or yes/no in it (or over
 * every column, as a CSV's header row is).
 */
function looksLikeHeader(sheet: Sheet) {
  const [first, second] = sheet.rows
  if (!first || !second) return false
  const filled = first.filter((cell): cell is Cell => Boolean(cell))
  if (filled.length < Math.max(1, Math.ceil(sheet.columns / 2))) return false
  if (!filled.every((cell) => typeof cell.value === "string")) return false
  if (new Set(filled.map((cell) => cell.text)).size !== filled.length)
    return false
  const valuesBelow = second.some(
    (cell) => cell !== undefined && typeof cell.value !== "string"
  )
  return valuesBelow || filled.length === sheet.columns
}

const exportName = (file: string, sheet?: string) => {
  const base = file.replace(/\.[^.]+$/, "")
  return sheet ? `${base} - ${sheet}` : base
}

const formatCount = (value: number) => value.toLocaleString()
