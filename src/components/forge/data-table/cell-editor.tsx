import * as React from "react"
import { cn } from "cn"
import type { RowData } from "@tanstack/react-table"

import { Input } from "@/components/ui/input"
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { useDataTable } from "./context"
import type { Column, DataTableEditor, Row } from "./table-config"
import { getColumnLabel, inferEditor, toDate } from "./utils"

function toDraft(value: unknown, editor: DataTableEditor) {
  if (value === null || value === undefined) return ""
  if (editor === "date") {
    const date = toDate(value)
    return date ? date.toISOString().slice(0, 10) : ""
  }
  return String(value)
}

/** Converts the draft back to the original value's type. */
function fromDraft(draft: string, editor: DataTableEditor, previous: unknown) {
  if (editor === "number") {
    if (draft.trim() === "") return null
    return Number(draft)
  }
  if (editor === "date") {
    if (!draft) return null
    const date = new Date(`${draft}T00:00:00`)
    if (previous instanceof Date) return date
    if (typeof previous === "number") return date.getTime()
    return draft
  }
  return draft
}

/** In-cell editor. Enter commits, Tab commits and moves right, Escape cancels. */
function CellEditor({
  row,
  column,
}: {
  row: Row<RowData>
  column: Column<RowData, unknown>
}) {
  const { table, features, editing, commitEdit, stopEditing } = useDataTable()
  const meta = column.columnDef.meta
  const previous = row.getValue(column.id)
  const editor =
    meta?.editor ?? inferEditor(previous, Boolean(meta?.editorOptions))
  // Typing on a focused cell starts editing with that text.
  const initial = editing?.initial
  const [draft, setDraft] = React.useState(
    () => initial ?? toDraft(previous, editor)
  )
  const [error, setError] = React.useState<string | null>(null)
  // Unmounting blurs the input; don't commit twice (or after Escape).
  const done = React.useRef(false)

  const finish = (value: unknown, move?: "down" | "right") => {
    if (done.current) return
    if (
      editor === "number" &&
      typeof value === "number" &&
      Number.isNaN(value)
    ) {
      setError("Enter a number")
      return
    }
    const message = meta?.validate?.(value, row)
    if (message) {
      setError(message)
      return
    }
    done.current = true
    if (!Object.is(value, previous)) commitEdit(row, column.id, value)
    stopEditing()
    if (move && features.cellSelection) table.moveCellSelection(move)
  }

  const label = `Edit ${getColumnLabel(column)}`

  if (editor === "select") {
    const options = meta?.editorOptions ?? []
    const items = options.map((option) => ({
      value: String(option.value),
      label: option.label,
    }))
    return (
      <Select
        defaultOpen
        value={String(previous ?? "")}
        items={items}
        onValueChange={(key) => {
          const option = options.find((item) => String(item.value) === key)
          finish(option ? option.value : key)
        }}
        onOpenChange={(open) => {
          if (!open) stopEditing()
        }}
      >
        <SelectTrigger
          size="sm"
          aria-label={label}
          className="h-7 w-full text-xs"
        >
          <SelectValue />
        </SelectTrigger>
        <SelectContent alignItemWithTrigger={false}>
          <SelectGroup>
            {items.map((item) => (
              <SelectItem key={item.value} value={item.value}>
                {item.label}
              </SelectItem>
            ))}
          </SelectGroup>
        </SelectContent>
      </Select>
    )
  }

  return (
    <span className="relative block select-text">
      <Input
        autoFocus
        aria-label={label}
        aria-invalid={error ? true : undefined}
        type={
          editor === "number" ? "number" : editor === "date" ? "date" : "text"
        }
        inputMode={editor === "number" ? "decimal" : undefined}
        value={draft}
        onFocus={(event) => {
          if (initial === undefined) event.currentTarget.select()
        }}
        onChange={(event) => {
          setDraft(event.target.value)
          setError(null)
        }}
        onBlur={() => finish(fromDraft(draft, editor, previous))}
        onKeyDown={(event) => {
          event.stopPropagation()
          if (event.key === "Enter") {
            event.preventDefault()
            finish(fromDraft(draft, editor, previous), "down")
          } else if (event.key === "Tab") {
            event.preventDefault()
            finish(fromDraft(draft, editor, previous), "right")
          } else if (event.key === "Escape") {
            event.preventDefault()
            done.current = true
            stopEditing()
          }
        }}
        className={cn(
          "h-7 px-2 text-xs md:text-xs",
          meta?.align === "right" && "text-right tabular-nums"
        )}
      />
      {error ? (
        <span
          role="alert"
          className="absolute top-full left-0 z-10 mt-1 rounded-(--radius-item) border border-danger-border bg-danger-surface px-2 py-1 text-3xs whitespace-nowrap text-danger-foreground shadow-(--shadow-raised)"
        >
          {error}
        </span>
      ) : null}
    </span>
  )
}

export { CellEditor }
