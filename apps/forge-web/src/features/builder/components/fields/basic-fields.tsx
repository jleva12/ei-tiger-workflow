import * as React from "react"
import { cn } from "cn"

import { Icon } from "@/components/forge/icon"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Field, FieldDescription, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Textarea } from "@/components/ui/textarea"
import { IssueMessages } from "./field-issues"
import { useFieldIssues, useIssueLookup } from "./field-issues-context"

/*
 * The pieces a step's settings are made of, in every builder: labelled
 * text, number and choice fields, two side by side, and lists of rows.
 * Each names the setting it holds (`issue`): the step's issues about it
 * show under it, in place of its description, and an error turns it red.
 */

export const CODE = "font-mono text-xs md:text-xs"
// Code wraps at spaces and operators, never inside a name; a name longer
// than the field scrolls sideways.
export const CODE_WRAP = "overflow-x-auto whitespace-pre-wrap break-normal"

/** Under a field: its issues, else its description. */
function Below({
  issues,
  description,
}: {
  issues: ReturnType<typeof useFieldIssues>["issues"]
  description?: React.ReactNode
}) {
  if (issues.length) return <IssueMessages issues={issues} />
  return description ? <FieldDescription>{description}</FieldDescription> : null
}

export function TextField({
  label,
  value,
  onChange,
  placeholder,
  description,
  code,
  multiline,
  rows,
  issue,
}: {
  label: string
  value: string
  onChange: (value: string) => void
  placeholder?: string
  description?: React.ReactNode
  /** An expression or code: mono. */
  code?: boolean
  multiline?: boolean
  rows?: number
  /** The setting it holds, for its issues. */
  issue?: string
}) {
  const id = React.useId()
  const { issues, invalid, key } = useFieldIssues(issue)
  return (
    <Field data-invalid={invalid || undefined} data-field={key}>
      <FieldLabel htmlFor={id}>{label}</FieldLabel>
      {multiline ? (
        <Textarea
          id={id}
          aria-invalid={invalid || undefined}
          value={value}
          placeholder={placeholder}
          spellCheck={!code}
          onChange={(event) => onChange(event.target.value)}
          className={cn("max-h-72", code && CODE, code && CODE_WRAP)}
          style={rows ? { minHeight: `${rows * 1.5 + 1}rem` } : undefined}
        />
      ) : (
        <Input
          id={id}
          aria-invalid={invalid || undefined}
          value={value}
          placeholder={placeholder}
          spellCheck={!code}
          autoComplete="off"
          onChange={(event) => onChange(event.target.value)}
          className={cn(code && CODE)}
        />
      )}
      <Below issues={issues} description={description} />
    </Field>
  )
}

export function NumberField({
  label,
  hideLabel,
  value,
  onChange,
  min = 0,
  description,
  issue,
}: {
  label: string
  /** Named for screen readers only, where a heading above already names it. */
  hideLabel?: boolean
  value: number
  onChange: (value: number) => void
  min?: number
  description?: React.ReactNode
  /** The setting it holds, for its issues. */
  issue?: string
}) {
  const id = React.useId()
  const { issues, invalid, key } = useFieldIssues(issue)
  const [text, setText] = React.useState(String(value))
  const [shown, setShown] = React.useState(value)
  // Follow undo and redo, not the half-typed number.
  if (shown !== value) {
    setShown(value)
    if (Number(text) !== value) setText(String(value))
  }
  return (
    <Field data-invalid={invalid || undefined} data-field={key}>
      <FieldLabel htmlFor={id} className={cn(hideLabel && "sr-only")}>
        {label}
      </FieldLabel>
      <Input
        id={id}
        aria-invalid={invalid || undefined}
        type="number"
        inputMode="numeric"
        min={min}
        value={text}
        onChange={(event) => {
          setText(event.target.value)
          const next = Number(event.target.value)
          if (event.target.value !== "" && Number.isFinite(next) && next >= min)
            onChange(next)
        }}
        onBlur={() => setText(String(value))}
        className="tabular-nums"
      />
      <Below issues={issues} description={description} />
    </Field>
  )
}

export function ChoiceField<T extends string>({
  label,
  hideLabel,
  value,
  options,
  onChange,
  description,
  placeholder,
  issue,
}: {
  label: string
  /** Named for screen readers only, where a heading above already names it. */
  hideLabel?: boolean
  value: T
  options: { value: T; label: string }[]
  onChange: (value: T) => void
  description?: React.ReactNode
  placeholder?: string
  /** The setting it holds, for its issues. */
  issue?: string
}) {
  const id = React.useId()
  const { issues, invalid, key } = useFieldIssues(issue)
  return (
    <Field data-invalid={invalid || undefined} data-field={key}>
      <FieldLabel htmlFor={id} className={cn(hideLabel && "sr-only")}>
        {label}
      </FieldLabel>
      <Select
        items={options}
        value={value || null}
        onValueChange={(next) => {
          if (next) onChange(next as T)
        }}
      >
        <SelectTrigger
          id={id}
          aria-invalid={invalid || undefined}
          className="w-full"
        >
          <SelectValue placeholder={placeholder} />
        </SelectTrigger>
        <SelectContent alignItemWithTrigger={false}>
          {options.map((option) => (
            <SelectItem key={option.value} value={option.value}>
              {option.label}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      <Below issues={issues} description={description} />
    </Field>
  )
}

/** A number the model has a default for: empty uses it. */
export function OptionalNumberField({
  label,
  value,
  onChange,
  min,
  max,
  integer,
  placeholder,
  issue,
}: {
  label: string
  value: number | null
  onChange: (value: number | null) => void
  min: number
  max?: number
  integer?: boolean
  placeholder: string
  /** The setting it holds, for its issues. */
  issue?: string
}) {
  const id = React.useId()
  const { issues, invalid, key } = useFieldIssues(issue)
  const [text, setText] = React.useState(value === null ? "" : String(value))
  const [shown, setShown] = React.useState(value)
  // Follow undo and redo, not the half-typed number.
  if (shown !== value) {
    setShown(value)
    if ((text.trim() === "" ? null : Number(text)) !== value)
      setText(value === null ? "" : String(value))
  }
  return (
    <Field data-invalid={invalid || undefined} data-field={key}>
      <FieldLabel htmlFor={id}>{label}</FieldLabel>
      <Input
        id={id}
        aria-invalid={invalid || undefined}
        type="number"
        inputMode={integer ? "numeric" : "decimal"}
        min={min}
        max={max}
        step={integer ? 1 : 0.1}
        value={text}
        placeholder={placeholder}
        onChange={(event) => {
          setText(event.target.value)
          if (event.target.value.trim() === "") return onChange(null)
          const next = Number(event.target.value)
          if (
            !Number.isFinite(next) ||
            next < min ||
            (max !== undefined && next > max)
          )
            return
          if (integer && !Number.isInteger(next)) return
          onChange(next)
        }}
        onBlur={() => setText(value === null ? "" : String(value))}
        className="tabular-nums"
      />
      <IssueMessages issues={issues} />
    </Field>
  )
}

export function CheckField({
  label,
  checked,
  onChange,
  description,
}: {
  label: string
  checked: boolean
  onChange: (checked: boolean) => void
  description?: React.ReactNode
}) {
  const id = React.useId()
  return (
    <Field orientation="horizontal" className="items-start">
      <Checkbox
        id={id}
        checked={checked}
        onCheckedChange={(on) => onChange(Boolean(on))}
        className="mt-0.5"
      />
      <div className="flex flex-col gap-1">
        <FieldLabel htmlFor={id} className="font-normal">
          {label}
        </FieldLabel>
        {description && <FieldDescription>{description}</FieldDescription>}
      </div>
    </Field>
  )
}

export function Row({ children }: { children: React.ReactNode }) {
  return <div className="grid grid-cols-2 gap-2.5">{children}</div>
}

/**
 * A list setting: one row each, its head beside the row's controls (move,
 * remove) and anything else below, then a dashed add button. A row's
 * issues show in it (`<setting>.<row id>`), the list's own under it.
 */
export function ListEditor<T extends { id: string }>({
  label,
  items,
  onChange,
  make,
  addLabel,
  renderItem,
  ordered,
  description,
  addDisabled,
  issue,
}: {
  label: string
  items: T[]
  onChange: (items: T[]) => void
  make: () => T
  addLabel: string
  /** Nothing more can be added (every value has its row). */
  addDisabled?: boolean
  renderItem: (
    item: T,
    change: (patch: Partial<T>) => void,
    index: number
  ) => { head: React.ReactNode; body?: React.ReactNode }
  /** The order matters: rows can move. */
  ordered?: boolean
  description?: React.ReactNode
  /** The setting it holds, for its issues and its rows'. */
  issue?: string
}) {
  const lookup = useIssueLookup()
  const own = lookup.of(issue)
  const move = (from: number, to: number) => {
    const next = [...items]
    const [moved] = next.splice(from, 1)
    next.splice(to, 0, moved)
    onChange(next)
  }
  return (
    <div
      role="group"
      aria-label={label}
      data-field={issue === undefined ? undefined : lookup.key(issue)}
      className="flex flex-col gap-2"
    >
      <span className="text-xs font-medium text-foreground">{label}</span>
      {items.map((item, index) => {
        const { head, body } = renderItem(
          item,
          (patch) =>
            onChange(
              items.map((x) => (x.id === item.id ? { ...x, ...patch } : x))
            ),
          index
        )
        const row = issue === undefined ? undefined : `${issue}.${item.id}`
        const issues = lookup.of(row)
        return (
          <div
            key={item.id}
            data-field={row === undefined ? undefined : lookup.key(row)}
            className={cn(
              "flex flex-col gap-1.5 rounded-(--radius-control) border p-1.5 pl-2",
              issues.some((i) => i.level === "error") && "border-destructive"
            )}
          >
            <div className="flex items-center gap-1">
              <div className="min-w-0 flex-1">{head}</div>
              {ordered && (
                <>
                  <Button
                    type="button"
                    variant="ghost"
                    size="icon-xs"
                    aria-label="Move up"
                    disabled={index === 0}
                    onClick={() => move(index, index - 1)}
                  >
                    <Icon icon="up" />
                  </Button>
                  <Button
                    type="button"
                    variant="ghost"
                    size="icon-xs"
                    aria-label="Move down"
                    disabled={index === items.length - 1}
                    onClick={() => move(index, index + 1)}
                  >
                    <Icon icon="down" />
                  </Button>
                </>
              )}
              <Button
                type="button"
                variant="ghost"
                size="icon-xs"
                aria-label="Remove"
                onClick={() => onChange(items.filter((x) => x.id !== item.id))}
                className="text-muted-foreground hover:text-destructive"
              >
                <Icon icon="close" />
              </Button>
            </div>
            {body}
            <IssueMessages issues={issues} className="px-1 pb-0.5" />
          </div>
        )
      })}
      <Button
        type="button"
        variant="outline"
        size="sm"
        disabled={addDisabled}
        onClick={() => onChange([...items, make()])}
        className="w-full border-dashed text-muted-foreground"
      >
        <Icon icon="plus" data-icon="inline-start" />
        {addLabel}
      </Button>
      {own.length ? (
        <IssueMessages issues={own} />
      ) : (
        description && (
          <p className="text-xs/[1.5] text-muted-foreground">{description}</p>
        )
      )}
    </div>
  )
}

export const bare =
  "h-7 border-0 bg-transparent px-1 shadow-none focus-visible:ring-0"
