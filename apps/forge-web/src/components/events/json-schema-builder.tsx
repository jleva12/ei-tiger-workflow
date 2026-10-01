import * as React from "react"

import { Icon } from "@/components/forge/icon"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { FieldError } from "@/components/ui/field"
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { Textarea } from "@/components/ui/textarea"
import {
  buildJsonSchema,
  checkDraft,
  countProperties,
  inferFromSample,
  parseJsonSchema,
  type SchemaDraft,
} from "@/lib/json-schema"
import { CopyButton, JsonView } from "./json-view"
import { BuilderContext, EVENT_SUBJECT, type SchemaSubject } from "./builder-context"
import { PropertyList } from "./schema-property-editor"

/**
 * Builds the JSON Schema an event's payload must match, a property at a
 * time: each with its name, type, whether it's required or may be null,
 * a description, a constant, and its type's constraints (lengths, pattern,
 * format and allowed values; ranges; items; nested properties, to any
 * depth). Properties collapse to `name : type` and reorder with the arrows.
 * Preview schema shows the JSON Schema it stands for; Import reads one in,
 * or drafts one from an example event.
 */
export function JsonSchemaBuilder({
  value,
  onChange,
  readOnly = false,
  title = "Payload schema",
  description,
  subject = EVENT_SUBJECT,
}: {
  value: SchemaDraft
  onChange: (draft: SchemaDraft) => void
  readOnly?: boolean
  title?: string
  description?: React.ReactNode
  /** What the schema describes, in its copy; events by default. */
  subject?: SchemaSubject
}) {
  const id = React.useId()
  const [previewing, setPreviewing] = React.useState(false)
  const [importing, setImporting] = React.useState(false)
  // Issues show once there's something to have them.
  const issues = React.useMemo(() => checkDraft(value), [value])
  const state = React.useMemo(() => ({ issues, readOnly, subject }), [issues, readOnly, subject])
  const topRequired = value.properties.filter((p) => p.required).length
  const { total } = countProperties(value)

  return (
    <BuilderContext value={state}>
      <section aria-labelledby={`${id}-title`} className="flex flex-col gap-4">
        <div className="flex items-start justify-between gap-4 @max-[600px]/shell:flex-wrap">
          <div className="min-w-0 flex-1 @max-[600px]/shell:basis-full">
            <div className="flex flex-wrap items-center gap-2">
              <h2 id={`${id}-title`} className="text-sm font-medium">
                {title}
              </h2>
              <Badge variant="secondary" className="font-normal tabular-nums">
                {value.properties.length}{" "}
                {value.properties.length === 1 ? "property" : "properties"}
              </Badge>
              {topRequired > 0 && (
                <Badge variant="outline" className="font-normal tabular-nums">
                  {topRequired} required
                </Badge>
              )}
              {total > value.properties.length && (
                <span className="text-xs text-subtle tabular-nums">
                  {total} in all
                </span>
              )}
            </div>
            {description && (
              <p className="mt-1 max-w-[70ch] text-xs text-muted-foreground">
                {description}
              </p>
            )}
          </div>
          <div className="-mr-2 flex shrink-0 items-center gap-1">
            {!readOnly && (
              <Button
                type="button"
                variant="ghost"
                size="sm"
                onClick={() => setImporting(true)}
              >
                <Icon icon="download" data-icon="inline-start" />
                Import
              </Button>
            )}
            <Button
              type="button"
              variant="ghost"
              size="sm"
              onClick={() => setPreviewing(true)}
            >
              <Icon icon="code" data-icon="inline-start" />
              Preview schema
            </Button>
          </div>
        </div>

        {value.properties.length === 0 && (
          <div className="rounded-(--radius-card) border border-dashed px-4 py-6 text-center text-xs text-muted-foreground">
            {readOnly
              ? "Any JSON object matches: the schema lists no properties."
              : `No properties yet. Add them one by one, or import a JSON Schema or an example ${subject.one} to start from.`}
          </div>
        )}
        <PropertyList
          properties={value.properties}
          depth={0}
          onChange={(properties) => onChange({ ...value, properties })}
        />
        <span className="flex items-center gap-2">
          <Checkbox
            id={`${id}-closed`}
            checked={!value.additionalProperties}
            disabled={readOnly}
            onCheckedChange={(closed) =>
              onChange({ ...value, additionalProperties: closed !== true })
            }
          />
          <label
            htmlFor={`${id}-closed`}
            className="text-xs text-muted-foreground select-none"
          >
            Refuse {subject.many} with properties the schema doesn&apos;t list
          </label>
        </span>
      </section>

      <SchemaPreviewDialog
        open={previewing}
        onOpenChange={setPreviewing}
        draft={value}
        subject={subject}
      />
      {!readOnly && (
        <SchemaImportDialog
          open={importing}
          onOpenChange={setImporting}
          replacing={value.properties.length > 0}
          onImport={onChange}
          subject={subject}
        />
      )}
    </BuilderContext>
  )
}

function SchemaPreviewDialog({
  open,
  onOpenChange,
  draft,
  subject,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  draft: SchemaDraft
  subject: SchemaSubject
}) {
  const schema = React.useMemo(() => buildJsonSchema(draft), [draft])
  const text = React.useMemo(() => JSON.stringify(schema, null, 2), [schema])
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="flex max-h-[80vh] flex-col sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Icon icon="code" size={16} />
            Generated JSON Schema
          </DialogTitle>
          <DialogDescription>
            Draft 2020-12.{" "}
            {subject === EVENT_SUBJECT
              ? "Every event of this type is checked against it when it arrives."
              : `Every ${subject.one} is checked against it.`}
          </DialogDescription>
        </DialogHeader>
        <JsonView value={schema} className="min-h-0 flex-1" />
        <DialogFooter>
          <CopyButton value={text} label="Copy schema" />
          <DialogClose render={<Button type="button" />}>Done</DialogClose>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

type ImportSource = "sample" | "schema"

const PLACEHOLDERS: Record<ImportSource, string> = {
  sample: `{
  "id": "INC-1042",
  "severity": "sev2",
  "opened_at": "2026-09-26T08:15:00Z",
  "service": { "name": "checkout" }
}`,
  schema: `{
  "type": "object",
  "properties": {
    "id": { "type": "string" }
  },
  "required": ["id"]
}`,
}

type ImportResult =
  { error: string } | { draft: SchemaDraft; warnings: string[] }

function readImport(
  source: ImportSource,
  text: string
): ImportResult | undefined {
  if (!text.trim()) return undefined
  let parsed: unknown
  try {
    parsed = JSON.parse(text)
  } catch (error) {
    return { error: `Not JSON: ${(error as Error).message}` }
  }
  if (source === "schema") {
    const { draft, warnings } = parseJsonSchema(parsed)
    return { draft, warnings }
  }
  try {
    return { draft: inferFromSample(parsed), warnings: [] }
  } catch (error) {
    return { error: (error as Error).message }
  }
}

function SchemaImportDialog({
  open,
  onOpenChange,
  replacing,
  onImport,
  subject,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** There are properties it replaces. */
  replacing: boolean
  onImport: (draft: SchemaDraft) => void
  subject: SchemaSubject
}) {
  const id = React.useId()
  const [source, setSource] = React.useState<ImportSource>("sample")
  const [text, setText] = React.useState("")
  const result = React.useMemo(() => readImport(source, text), [source, text])
  const draft = result && "draft" in result ? result.draft : undefined
  const warnings = result && "warnings" in result ? result.warnings : []

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        onOpenChange(next)
        if (!next) setText("")
      }}
    >
      <DialogContent className="flex max-h-[85vh] flex-col sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>Import a schema</DialogTitle>
          <DialogDescription>
            Paste an example {subject.one} to draft the schema from what it
            holds, or a JSON Schema you already have.
            {replacing && " It replaces the properties you have."}
          </DialogDescription>
        </DialogHeader>
        <Tabs
          value={source}
          onValueChange={(value) =>
            setSource(value === "schema" ? "schema" : "sample")
          }
        >
          <TabsList variant="line" aria-label="What you paste">
            <TabsTrigger value="sample">Example {subject.one}</TabsTrigger>
            <TabsTrigger value="schema">JSON Schema</TabsTrigger>
          </TabsList>
        </Tabs>
        <label htmlFor={`${id}-text`} className="sr-only">
          {source === "sample" ? `Example ${subject.one}` : "JSON Schema"}
        </label>
        <Textarea
          id={`${id}-text`}
          rows={12}
          value={text}
          spellCheck={false}
          placeholder={PLACEHOLDERS[source]}
          aria-invalid={Boolean(result && "error" in result)}
          onChange={(event) => setText(event.target.value)}
          className="min-h-48 font-mono text-xs md:text-xs"
        />
        {result && "error" in result && <FieldError>{result.error}</FieldError>}
        {draft && (
          <div className="flex flex-col gap-2 text-xs">
            <p className="text-muted-foreground">
              {source === "sample"
                ? `Reads ${draft.properties.length} properties, with the types and formats the example shows. None is required yet: mark the ones every ${subject.one} has.`
                : `Reads ${draft.properties.length} properties.`}
            </p>
            {warnings.length > 0 && (
              <div className="rounded-(--radius-control) bg-warning-surface px-3 py-2 text-warning-foreground">
                <p className="mb-1 font-medium">
                  Some of it can't be built here and is left out:
                </p>
                <ul className="max-h-28 list-disc overflow-auto pl-4">
                  {warnings.map((warning) => (
                    <li key={warning}>{warning}</li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        )}
        <DialogFooter>
          <DialogClose render={<Button type="button" variant="outline" />}>
            Cancel
          </DialogClose>
          <Button
            type="button"
            disabled={!draft}
            onClick={() => {
              if (!draft) return
              onImport(draft)
              onOpenChange(false)
              setText("")
            }}
          >
            {replacing ? "Replace properties" : "Use these properties"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
