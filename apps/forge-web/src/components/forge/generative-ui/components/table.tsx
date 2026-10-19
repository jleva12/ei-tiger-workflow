import * as React from "react"
import { cn } from "cn"
import { Icon } from "@/components/forge/icon"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { isText } from "../value"

type Format = { kind?: unknown; currency?: unknown; decimals?: unknown }
type Column = { label: string; align?: unknown; format?: Format }
type Cell = string | number | boolean

function formatCell(value: unknown, format: Format | undefined): string {
  if (typeof value === "boolean") return value ? "Yes" : "No"
  if (!isText(value)) return ""
  if (!format) return typeof value === "number" ? value.toLocaleString() : value
  const decimals =
    typeof format.decimals === "number" &&
    format.decimals >= 0 &&
    format.decimals <= 6
      ? format.decimals
      : undefined
  try {
    if (format.kind === "date") {
      const date = new Date(value)
      return Number.isNaN(date.getTime())
        ? String(value)
        : date.toLocaleDateString()
    }
    const number = typeof value === "number" ? value : Number(value)
    if (!Number.isFinite(number)) return String(value)
    return new Intl.NumberFormat(undefined, {
      ...(format.kind === "currency" && {
        style: "currency",
        currency: typeof format.currency === "string" ? format.currency : "USD",
      }),
      ...(format.kind === "percent" && { style: "percent" }),
      ...(decimals !== undefined && {
        minimumFractionDigits: decimals,
        maximumFractionDigits: decimals,
      }),
    }).format(number)
  } catch {
    // A currency code Intl doesn't know.
    return String(value)
  }
}

const compareCells = (a: Cell | undefined, b: Cell | undefined) =>
  typeof a === "number" && typeof b === "number"
    ? a - b
    : String(a ?? "").localeCompare(String(b ?? ""), undefined, {
        numeric: true,
      })

export function RichTable({
  columns,
  rows,
  sortable,
}: {
  columns?: unknown
  rows?: unknown
  sortable?: unknown
}) {
  const [sort, setSort] = React.useState<{ index: number; up: boolean }>()
  const heads = (Array.isArray(columns) ? columns : []).filter(
    (column): column is Column =>
      typeof column === "object" && column !== null && isText(column.label)
  )
  const body = (Array.isArray(rows) ? rows : []).filter((row): row is Cell[] =>
    Array.isArray(row)
  )
  const width = Math.max(heads.length, ...body.map((row) => row.length))
  if (!width) return null
  const sorted = sort
    ? [...body].sort(
        (a, b) =>
          compareCells(a[sort.index], b[sort.index]) * (sort.up ? 1 : -1)
      )
    : body
  const end = (index: number) => heads[index]?.align === "end"
  const canSort = sortable === true
  return (
    <Table data-slot="rich-table" className="text-xs">
      {heads.length > 0 && (
        <TableHeader>
          <TableRow className="hover:bg-transparent">
            {Array.from({ length: width }, (_, index) => {
              const label = heads[index]?.label ?? ""
              const by = sort?.index === index
              return (
                <TableHead
                  key={index}
                  aria-sort={
                    by ? (sort.up ? "ascending" : "descending") : undefined
                  }
                  className={cn(
                    "h-8 px-2 text-2xs font-medium text-muted-foreground first:pl-0 last:pr-0",
                    end(index) && "text-right"
                  )}
                >
                  {canSort ? (
                    <button
                      type="button"
                      className="inline-flex items-center gap-1 hover:text-foreground"
                      onClick={() =>
                        setSort({ index, up: by ? !sort.up : true })
                      }
                    >
                      {label}
                      {by && (
                        <Icon
                          icon={sort.up ? "up" : "down"}
                          className="size-3"
                        />
                      )}
                    </button>
                  ) : (
                    label
                  )}
                </TableHead>
              )
            })}
          </TableRow>
        </TableHeader>
      )}
      <TableBody>
        {sorted.map((row, rowIndex) => (
          <TableRow key={rowIndex} className="hover:bg-transparent">
            {Array.from({ length: width }, (_, index) => (
              <TableCell
                key={index}
                className={cn(
                  "px-2 py-1.5 tabular-nums first:pl-0 last:pr-0",
                  end(index) && "text-right"
                )}
              >
                {formatCell(row[index], heads[index]?.format)}
              </TableCell>
            ))}
          </TableRow>
        ))}
      </TableBody>
    </Table>
  )
}
