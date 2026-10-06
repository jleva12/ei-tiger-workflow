import * as React from "react"

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
import { useCompanionCard } from "@/features/builder/components/settings-companion"
import { SettingsCompanion } from "@/features/builder/components/settings-dialog"
import { IssueMessages } from "./field-issues"
import { useFieldIssues } from "./field-issues-context"

/**
 * A setting that declares the shape of some data (the input every run
 * starts with, the JSON an agent returns) as a JSON Schema. The step's
 * settings list its fields with their types; the fields are built in a
 * card beside the card it's in (json/components/json-schema-builder). What it
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
  const key = React.useId()
  const editor = useCompanionCard(key)
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
    editor.show()
  }

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
          type="button"
          variant="outline"
          size="xs"
          aria-expanded={editor.open}
          disabled={!editor.canOpen}
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

      {/* Gone while open (its step's settings changed), it closes too. */}
      <SettingsCompanion
        id={key}
        title={title}
        description={description}
        closeLabel="Close without saving"
        render={
          <form
            onSubmit={(event) => {
              event.preventDefault()
              // Empty and open is the same as undeclared; empty and closed allows nothing.
              onChange(
                draft.properties.length || !draft.additionalProperties ? buildJsonSchema(draft) : {}
              )
              editor.hide()
            }}
          />
        }
        bodyClassName="flex flex-col gap-5 px-5 py-4"
        footer={
          <>
            <span className="flex items-center gap-1.5 text-2xs text-subtle max-[600px]:hidden">
              <Icon icon="code" size={14} />
              Nothing changes until you save
            </span>
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="ml-auto"
              onClick={() => editor.hide()}
            >
              Cancel
            </Button>
            <Button type="submit" size="sm">
              Save fields
            </Button>
          </>
        }
      >
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
      </SettingsCompanion>
    </div>
  )
}
