import * as React from "react"
import { createPortal } from "react-dom"
import { cn } from "cn"

import type { SchemaSubject } from "@/features/json/components/schema-builder-context"
import { JsonSchemaBuilder } from "@/features/json/components/json-schema-builder"
import { Icon } from "@/components/forge/icon"
import { Button } from "@/components/ui/button"
import {
  buildJsonSchema,
  emptyDraft,
  parseJsonSchema,
  type SchemaDraft,
} from "@/features/json/lib/json-schema"
import { fromJsonSchema, typeLabel } from "@/features/steps/lib/types"
import { DIALOG_CARD, useCompanion } from "@/features/builder/components/utils"
import { IssueMessages } from "./field-issues"
import { useFieldIssues } from "./field-issues-context"

/**
 * A setting that declares the shape of some data (the input every run
 * starts with, the JSON an agent returns) as a JSON Schema. The step's
 * settings list its fields with their types; the fields are built in a
 * card beside the settings (json/components/json-schema-builder). What it
 * declares is what later steps' fields complete and check against.
 */
export function SchemaField({
  label,
  title,
  description,
  schema,
  onChange,
  empty,
  subject,
  issue,
}: {
  label: string
  /** The editor's title. */
  title: string
  description: React.ReactNode
  /** The JSON Schema; `{}` or undefined declares nothing. */
  schema: Record<string, unknown> | undefined
  onChange: (schema: Record<string, unknown>) => void
  /** What the setting says with nothing declared. */
  empty: string
  /** What the data is, in the builder's copy: an input, an answer. */
  subject: SchemaSubject
  /** The setting it holds, for its issues. */
  issue?: string
}) {
  const { issues, key: field } = useFieldIssues(issue)
  const companion = useCompanion()
  const key = React.useId()
  const titleId = React.useId()
  const descriptionId = React.useId()
  const open = companion?.open === key
  const [draft, setDraft] = React.useState<SchemaDraft>(emptyDraft)
  const [warnings, setWarnings] = React.useState<string[]>([])
  const declared = schema && Object.keys(schema).length > 0
  const type = declared ? fromJsonSchema(schema) : undefined
  // A field still being named isn't one yet.
  const fields =
    type?.kind === "object" ? Object.entries(type.properties).filter(([name]) => name.trim()) : []
  const closed = type?.kind === "object" && !type.open
  const required = new Set(type?.kind === "object" ? (type.required ?? []) : [])

  const edit = () => {
    const parsed = declared ? parseJsonSchema(schema) : { draft: emptyDraft(), warnings: [] }
    setDraft(parsed.draft)
    setWarnings(parsed.warnings)
    companion?.show(key)
  }

  // Focus follows the editor: into it as it opens, back to its button as it closes.
  const button = React.useRef<HTMLButtonElement>(null)
  const panel = React.useRef<HTMLFormElement>(null)
  const wasOpen = React.useRef(false)
  React.useEffect(() => {
    if (open) panel.current?.focus()
    else if (wasOpen.current) button.current?.focus()
    wasOpen.current = open
  }, [open])
  // Gone while its editor is open (its step's settings changed): the editor goes too.
  const release = companion?.release
  React.useEffect(() => () => release?.(key), [release, key])

  return (
    <div
      role="group"
      aria-label={label}
      data-field={field}
      className="flex flex-col gap-2"
    >
      <div className="flex items-center justify-between gap-2">
        <span className="text-xs font-medium text-foreground">{label}</span>
        <Button
          ref={button}
          type="button"
          variant="outline"
          size="xs"
          aria-expanded={open}
          onClick={edit}
        >
          <Icon icon={declared ? "settings" : "plus"} data-icon="inline-start" />
          {declared ? "Edit fields" : "Declare fields"}
        </Button>
      </div>
      {fields.length ? (
        <ul className="flex flex-col rounded-(--radius-control) border">
          {fields.map(([name, field]) => (
            <li
              key={name}
              className="flex items-baseline gap-3 border-t px-2.5 py-1.5 first:border-t-0"
            >
              <code className="min-w-0 truncate font-mono text-xs text-foreground">{name}</code>
              {required.has(name) && <span className="text-2xs text-subtle">required</span>}
              <span className="ml-auto shrink-0 font-mono text-2xs text-muted-foreground">
                {typeLabel(field)}
              </span>
            </li>
          ))}
        </ul>
      ) : (
        <p className="text-xs/[1.6] text-muted-foreground">
          {!declared
            ? empty
            : closed
              ? "It declares no fields, and allows none."
              : "It declares no fields: anything goes."}
        </p>
      )}
      <IssueMessages issues={issues} />

      {open &&
        companion.slot &&
        createPortal(
          <form
            ref={panel}
            tabIndex={-1}
            aria-labelledby={titleId}
            aria-describedby={descriptionId}
            className={cn(
              DIALOG_CARD,
              "w-[47.5rem] min-w-[28rem] shrink [view-transition-name:step-companion]",
              // Too narrow for two: it covers the settings.
              "max-[1080px]:absolute max-[1080px]:inset-0 max-[1080px]:w-auto max-[1080px]:min-w-0"
            )}
            onSubmit={(event) => {
              event.preventDefault()
              // Empty and open is the same as undeclared; empty and closed allows nothing.
              onChange(
                draft.properties.length || !draft.additionalProperties ? buildJsonSchema(draft) : {}
              )
              companion.hide()
            }}
          >
            <header className="flex items-start gap-3 border-b px-5 pt-4 pb-3.5">
              <div className="flex min-w-0 flex-1 flex-col gap-0.5">
                <h2 id={titleId} className="truncate text-sm leading-snug font-medium text-foreground">
                  {title}
                </h2>
                <p id={descriptionId} className="max-w-[36rem] text-xs/[1.6] text-muted-foreground">
                  {description}
                </p>
              </div>
              <Button
                type="button"
                variant="ghost"
                size="icon-sm"
                aria-label="Close without saving"
                onClick={() => companion.hide()}
                className="-mt-0.5 -mr-1.5 text-muted-foreground"
              >
                <Icon icon="close" />
              </Button>
            </header>
            <div className="flex min-h-0 flex-1 flex-col gap-5 overflow-y-auto overscroll-contain px-5 py-4">
              {warnings.length > 0 && (
                <p className="flex items-start gap-2 text-xs/[1.6] text-tone-amber-foreground">
                  <Icon icon="warning" size={14} className="mt-0.5 shrink-0" />
                  <span>
                    Some of it can&apos;t be edited here and is left out when you save:{" "}
                    {warnings.join("; ")}.
                  </span>
                </p>
              )}
              <JsonSchemaBuilder
                value={draft}
                onChange={setDraft}
                subject={subject}
                title="Fields"
                description="Each field, its type and whether it's always there. Later steps complete and check against these."
              />
            </div>
            <footer className="flex items-center gap-2 border-t px-5 py-3">
              <span className="flex items-center gap-1.5 text-2xs text-subtle max-[600px]:hidden">
                <Icon icon="code" size={14} />
                Nothing changes until you save
              </span>
              <Button
                type="button"
                variant="outline"
                size="sm"
                className="ml-auto"
                onClick={() => companion.hide()}
              >
                Cancel
              </Button>
              <Button type="submit" size="sm">
                Save fields
              </Button>
            </footer>
          </form>,
          companion.slot
        )}
    </div>
  )
}
