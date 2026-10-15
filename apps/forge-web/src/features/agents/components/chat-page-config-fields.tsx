import * as React from "react"

import { Icon } from "@/components/forge/icon"
import { Button } from "@/components/ui/button"
import { Field, FieldDescription, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { Switch } from "@/components/ui/switch"
import { Textarea } from "@/components/ui/textarea"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import { cn } from "@/lib/utils"
import {
  ACCENTS,
  cleanedPrompts,
  isAccent,
  MAX_PAGE_SUGGESTIONS,
} from "@/features/agents/lib/page-settings"

/*
 * The configuration page's fields: a section, a text, one of a few
 * choices, the accent, the prompt suggestions. Each says what it changes
 * at once; saving is the page's.
 */

/** A part of the settings, under a heading. */
export function ConfigSection({
  title,
  description,
  children,
}: {
  title: string
  description?: string
  children: React.ReactNode
}) {
  const id = React.useId()
  return (
    <section
      aria-labelledby={id}
      className="flex flex-col gap-3.5 border-b px-4 py-4 last:border-b-0"
    >
      <div className="flex flex-col gap-0.5">
        <h2 id={id} className="text-xs font-semibold text-foreground">
          {title}
        </h2>
        {description && (
          <p className="text-2xs/[1.5] text-muted-foreground">{description}</p>
        )}
      </div>
      {children}
    </section>
  )
}

/** A text the page shows, with its default as the placeholder. */
export function TextField({
  label,
  description,
  value,
  placeholder,
  maxLength,
  multiline = false,
  disabled,
  onChange,
}: {
  label: string
  description?: string
  value: string
  placeholder: string
  maxLength: number
  multiline?: boolean
  disabled: boolean
  onChange: (value: string) => void
}) {
  const id = React.useId()
  const near = value.length > maxLength * 0.8
  return (
    <Field>
      <div className="flex items-baseline justify-between gap-2">
        <FieldLabel htmlFor={id} className="text-xs">
          {label}
        </FieldLabel>
        {near && (
          <span className="text-3xs text-muted-foreground tabular-nums">
            {value.length}/{maxLength}
          </span>
        )}
      </div>
      {multiline ? (
        <Textarea
          id={id}
          value={value}
          placeholder={placeholder}
          maxLength={maxLength}
          disabled={disabled}
          rows={3}
          className="min-h-16 text-xs"
          onChange={(event) => onChange(event.target.value)}
        />
      ) : (
        <Input
          id={id}
          value={value}
          placeholder={placeholder}
          maxLength={maxLength}
          disabled={disabled}
          className="h-8 text-xs"
          onChange={(event) => onChange(event.target.value)}
        />
      )}
      {description && (
        <FieldDescription className="text-2xs">{description}</FieldDescription>
      )}
    </Field>
  )
}

/** A setting that's on or off, with what it does under it. */
export function SwitchField({
  label,
  description,
  checked,
  disabled,
  onChange,
}: {
  label: string
  description: string
  checked: boolean
  disabled: boolean
  onChange: (checked: boolean) => void
}) {
  const id = React.useId()
  return (
    <div className="flex items-start justify-between gap-3">
      <div className="flex min-w-0 flex-col gap-0.5">
        <label htmlFor={id} className="text-xs font-medium text-foreground">
          {label}
        </label>
        <p className="text-2xs/[1.5] text-muted-foreground">{description}</p>
      </div>
      <Switch
        id={id}
        className="mt-0.5"
        checked={checked}
        disabled={disabled}
        onCheckedChange={onChange}
      />
    </div>
  )
}

/** One of a few choices, side by side. */
export function ChoiceField<T extends string>({
  label,
  value,
  options,
  disabled,
  onChange,
}: {
  label: string
  value: T
  options: readonly { value: T; label: string; hint?: string }[]
  disabled: boolean
  onChange: (value: T) => void
}) {
  const id = React.useId()
  const hint = options.find((option) => option.value === value)?.hint
  return (
    <div className="flex flex-col gap-1.5">
      <span id={id} className="text-xs font-medium text-foreground">
        {label}
      </span>
      <ToggleGroup
        aria-labelledby={id}
        value={[value]}
        disabled={disabled}
        onValueChange={(next) => {
          if (next[0]) onChange(next[0] as T)
        }}
        spacing={0}
        className="flex w-full rounded-(--radius-control) bg-muted p-[3px]"
      >
        {options.map((option) => (
          <ToggleGroupItem
            key={option.value}
            value={option.value}
            className="h-[26px] min-w-0 flex-1 rounded-(--radius-soft)! px-2 text-xs font-normal text-muted-foreground hover:bg-transparent hover:text-foreground aria-pressed:bg-background aria-pressed:text-foreground aria-pressed:shadow-(--shadow-raised)"
          >
            {option.label}
          </ToggleGroupItem>
        ))}
      </ToggleGroup>
      {hint && <p className="text-2xs/[1.5] text-muted-foreground">{hint}</p>}
    </div>
  )
}

/** The accent: one of a few at a click, or any colour picked or typed. */
export function AccentField({
  value,
  disabled,
  onChange,
}: {
  value: string | null
  disabled: boolean
  onChange: (value: string | null) => void
}) {
  const id = React.useId()
  // What's typed, until it's a colour.
  const [typed, setTyped] = React.useState<string>()
  const shown = typed ?? value ?? ""
  const named = ACCENTS.find((accent) => accent.value === value)
  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-baseline justify-between gap-2">
        <span id={id} className="text-xs font-medium text-foreground">
          Accent colour
        </span>
        <span className="text-2xs text-muted-foreground">
          {named ? named.name : "Custom"}
        </span>
      </div>
      <div role="group" aria-labelledby={id} className="flex flex-wrap gap-1.5">
        {ACCENTS.map((accent) => (
          <button
            key={accent.name}
            type="button"
            disabled={disabled}
            aria-pressed={accent.value === value}
            aria-label={accent.name}
            title={accent.name}
            onClick={() => {
              setTyped(undefined)
              onChange(accent.value)
            }}
            className={cn(
              "grid size-6 place-items-center rounded-full ring-offset-2 ring-offset-background transition-shadow disabled:opacity-50 aria-pressed:ring-2 aria-pressed:ring-foreground/70",
              accent.value === null &&
                "bg-[conic-gradient(var(--color-foreground)_0_50%,var(--color-background)_0)] shadow-[inset_0_0_0_1px_var(--color-border)]"
            )}
            style={accent.value ? { background: accent.value } : undefined}
          />
        ))}
      </div>
      <div className="flex items-center gap-2">
        <label
          className={cn(
            "relative size-8 shrink-0 cursor-pointer overflow-hidden rounded-(--radius-control) border",
            disabled && "pointer-events-none opacity-50"
          )}
          style={{ background: value ?? "var(--color-primary)" }}
          title="Pick a colour"
        >
          <span className="sr-only">Pick a colour</span>
          <input
            type="color"
            className="absolute inset-0 size-full cursor-pointer opacity-0"
            value={value ?? "#2563eb"}
            disabled={disabled}
            onChange={(event) => {
              setTyped(undefined)
              onChange(event.target.value.toLowerCase())
            }}
          />
        </label>
        <Input
          aria-label="Accent colour, as #rrggbb"
          value={shown}
          placeholder="Forge ink"
          maxLength={7}
          disabled={disabled}
          aria-invalid={typed !== undefined && !isAccent(typed) && typed !== ""}
          className="h-8 font-mono text-xs"
          onChange={(event) => {
            const next = event.target.value.trim()
            if (next === "") {
              setTyped(undefined)
              onChange(null)
            } else if (isAccent(next)) {
              setTyped(undefined)
              onChange(next.toLowerCase())
            } else setTyped(next)
          }}
          onBlur={() => setTyped(undefined)}
        />
      </div>
    </div>
  )
}

/**
 * The prompts an empty conversation offers: rows to write, add and remove.
 * Each change hands on the list as it'd be saved; remount it (its `key`)
 * when the list is set from outside, e.g. discarding changes.
 */
export function SuggestionsField({
  initial,
  disabled,
  onChange,
}: {
  initial: readonly string[]
  disabled: boolean
  onChange: (prompts: string[]) => void
}) {
  const [rows, setRows] = React.useState<string[]>(() =>
    initial.length ? [...initial] : [""]
  )
  // A row added from the list (Add, or Enter on the last) takes the focus.
  const [added, setAdded] = React.useState<number>()
  const change = (next: string[]) => {
    setRows(next)
    onChange(cleanedPrompts(next))
  }
  const full = rows.length >= MAX_PAGE_SUGGESTIONS
  const add = () => {
    if (full) return
    setAdded(rows.length)
    setRows([...rows, ""])
  }
  return (
    <div className="flex flex-col gap-1.5">
      {rows.map((row, index) => (
        <div key={index} className="flex items-center gap-1.5">
          <Input
            autoFocus={index === added}
            value={row}
            maxLength={200}
            disabled={disabled}
            aria-label={`Suggestion ${index + 1}`}
            placeholder={
              index === 0 ? "e.g. What can you help me with?" : "Another prompt"
            }
            className="h-8 text-xs"
            onChange={(event) =>
              change(
                rows.map((each, at) =>
                  at === index ? event.target.value : each
                )
              )
            }
            onKeyDown={(event) => {
              if (event.key === "Enter" && index === rows.length - 1 && row) {
                event.preventDefault()
                add()
              }
            }}
          />
          <Button
            variant="ghost"
            size="icon-xs"
            aria-label={`Remove suggestion ${index + 1}`}
            disabled={disabled}
            onClick={() => change(rows.filter((_, at) => at !== index))}
          >
            <Icon icon="close" />
          </Button>
        </div>
      ))}
      <div className="flex items-center justify-between gap-2">
        <Button
          variant="outline"
          size="xs"
          disabled={disabled || full}
          onClick={add}
        >
          <Icon icon="plus" data-icon="inline-start" />
          Add suggestion
        </Button>
        <span className="text-2xs text-muted-foreground tabular-nums">
          {cleanedPrompts(rows).length} of {MAX_PAGE_SUGGESTIONS}
        </span>
      </div>
    </div>
  )
}
