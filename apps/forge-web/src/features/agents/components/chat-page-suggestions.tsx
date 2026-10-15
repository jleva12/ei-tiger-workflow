import * as React from "react"

import { Icon } from "@/components/forge/icon"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Spinner } from "@/components/ui/spinner"
import { MAX_PAGE_SUGGESTIONS } from "@/features/agents/lib/api"

/** The prompts as saved: trimmed, without blanks, each once. */
const cleaned = (rows: readonly string[]) => [
  ...new Set(rows.map((row) => row.trim()).filter(Boolean)),
]

const same = (a: readonly string[], b: readonly string[]) =>
  a.length === b.length && a.every((value, index) => value === b[index])

/**
 * The prompts a chat page offers under its message box while a
 * conversation is empty: a list to write, reorder by rewriting, and save.
 * Remount it with the saved list as its key, so a save starts it afresh.
 */
export function ChatPageSuggestions({
  saved,
  canManage,
  saving,
  onSave,
}: {
  saved: readonly string[]
  canManage: boolean
  saving: boolean
  onSave: (suggestions: string[]) => void
}) {
  const id = React.useId()
  const [rows, setRows] = React.useState<string[]>(() =>
    saved.length ? [...saved] : canManage ? [""] : []
  )
  // A row added from the list (Add, or Enter on the last) takes the focus.
  const [added, setAdded] = React.useState<number>()
  const next = cleaned(rows)
  const changed = !same(next, saved)
  const full = rows.length >= MAX_PAGE_SUGGESTIONS
  const add = () => {
    if (full) return
    setAdded(rows.length)
    setRows([...rows, ""])
  }
  return (
    <div
      role="group"
      aria-labelledby={`${id}-label`}
      className="flex flex-col gap-2 border-t pt-2.5"
    >
      <div className="flex items-baseline justify-between gap-2">
        <span
          id={`${id}-label`}
          className="text-xs font-medium text-foreground"
        >
          Prompt suggestions
        </span>
        <span className="text-2xs text-muted-foreground tabular-nums">
          {next.length} of {MAX_PAGE_SUGGESTIONS}
        </span>
      </div>
      <p className="text-xs/[1.6] text-muted-foreground">
        Shown under the message box while a conversation is empty: a click sends
        one.
      </p>
      {rows.map((row, index) => (
        <div key={index} className="flex items-center gap-1.5">
          <Input
            // Only a row just added mounts focused.
            autoFocus={index === added}
            value={row}
            maxLength={200}
            disabled={!canManage}
            aria-label={`Suggestion ${index + 1}`}
            placeholder={
              index === 0 ? "e.g. What can you help me with?" : "Another prompt"
            }
            onChange={(event) =>
              setRows(
                rows.map((each, at) =>
                  at === index ? event.target.value : each
                )
              )
            }
            onKeyDown={(event) => {
              // Enter on the last one starts another.
              if (event.key === "Enter" && index === rows.length - 1 && row) {
                event.preventDefault()
                add()
              }
            }}
          />
          {canManage && (
            <Button
              variant="ghost"
              size="icon-xs"
              aria-label={`Remove suggestion ${index + 1}`}
              onClick={() => setRows(rows.filter((_, at) => at !== index))}
            >
              <Icon icon="close" />
            </Button>
          )}
        </div>
      ))}
      {canManage && (
        <div className="flex gap-1.5">
          <Button variant="outline" size="xs" disabled={full} onClick={add}>
            <Icon icon="plus" data-icon="inline-start" />
            Add suggestion
          </Button>
          <Button
            size="xs"
            disabled={!changed || saving}
            onClick={() => onSave(next)}
          >
            {saving && <Spinner data-icon="inline-start" />}
            Save
          </Button>
        </div>
      )}
    </div>
  )
}
