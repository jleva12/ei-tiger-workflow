import * as React from "react"
import { cn } from "cn"

import { Icon } from "@/components/forge/icon"
import { Button } from "@/components/ui/button"
import { FieldDescription, FieldLabel } from "@/components/ui/field"
import { Skeleton } from "@/components/ui/skeleton"
import { useCompanionCard } from "@/features/builder/components/settings-companion"
import { SettingsCompanion } from "@/features/builder/components/settings-dialog"
import type { BuilderIssue } from "@/features/builder/lib/types"
import { checkField, type FieldCheck } from "@/features/steps/lib/expressions"
import type { Scope } from "@/features/steps/lib/scope"
import { IssueMessages } from "./field-issues"
import type { TemplateAI, TemplateWriter } from "./markdown-template-editor"
import { FieldsReadOnly } from "./read-only"

// Tiptap comes with the first preview or editor that shows, not the dialog.
const load = () => import("./markdown-template-editor")
const TemplatePreview = React.lazy(() =>
  load().then((module) => ({ default: module.TemplatePreview }))
)
const TemplateEditor = React.lazy(() =>
  load().then((module) => ({ default: module.TemplateEditor }))
)

/** Lines of the preview before it fades out. */
const PREVIEW_LINES = 5

type Problem = { severity: "error" | "warning"; message: string }

/** The template's problems, errors first, as a field shows them. */
function Problems({ problems }: { problems: Problem[] }) {
  const shown = [
    ...problems.filter((p) => p.severity === "error"),
    ...problems.filter((p) => p.severity === "warning"),
  ].slice(0, 2)
  return (
    <>
      {shown.map((problem, index) => (
        <p
          key={index}
          className={cn(
            "flex items-start gap-1.5 text-xs/[1.5]",
            problem.severity === "error"
              ? "text-destructive"
              : "text-tone-amber-foreground"
          )}
        >
          <Icon
            icon={problem.severity === "error" ? "failed" : "warning"}
            size={13}
            className="mt-0.5 shrink-0"
          />
          {problem.message}
        </p>
      ))}
      {problems.length > shown.length && (
        <p className="text-2xs text-muted-foreground">
          {problems.length - shown.length} more; open the editor to see them
          all.
        </p>
      )}
    </>
  )
}

/**
 * The first lines of the setting, formatted and read-only, fading out when
 * there's more. Clicking it opens the editor too.
 */
function Preview({
  value,
  label,
  placeholder,
  onOpen,
}: {
  value: string
  label: string
  placeholder?: string
  onOpen: () => void
}) {
  const box = React.useRef<HTMLDivElement>(null)
  const content = React.useRef<HTMLDivElement>(null)
  const [overflowing, setOverflowing] = React.useState(false)
  React.useEffect(() => {
    const outer = box.current
    const inner = content.current
    if (!outer || !inner) return
    const measure = () =>
      setOverflowing(inner.scrollHeight > outer.clientHeight + 1)
    const observer = new ResizeObserver(measure)
    observer.observe(outer)
    observer.observe(inner)
    return () => observer.disconnect()
  }, [])

  return (
    <div
      className="template-preview group relative cursor-pointer rounded-(--radius-control) border bg-muted/30 transition-colors hover:border-ring/60"
      onClick={onOpen}
    >
      <div
        ref={box}
        className="overflow-hidden px-3 py-2"
        // Five lines of 0.8125rem text at 1.6, and the padding.
        style={{ maxHeight: `calc(${PREVIEW_LINES} * 1.3rem + 1rem)` }}
      >
        <div ref={content}>
          {value.trim() ? (
            <React.Suspense
              fallback={
                <div aria-busy="true" className="flex flex-col gap-1.5 py-0.5">
                  <Skeleton className="h-3.5 w-3/4" />
                  <Skeleton className="h-3.5 w-1/2" />
                </div>
              }
            >
              <TemplatePreview value={value} label={label} />
            </React.Suspense>
          ) : (
            <p className="text-xs/[1.6] text-subtle">
              {placeholder ?? "Nothing written yet."}
            </p>
          )}
        </div>
      </div>
      {overflowing && (
        <div
          aria-hidden="true"
          className="pointer-events-none absolute inset-x-px bottom-px h-8 rounded-b-[inherit] bg-linear-to-t from-background to-transparent"
        />
      )}
    </div>
  )
}

/**
 * A long template setting in Markdown (an agent's instruction): its first
 * lines read-only under the label, and Open editor for the whole of it in a
 * wide card beside, in the rich text editor, where `{{ … }}` puts in the
 * run's data as chips. As with a schema, nothing changes until it's saved.
 * Its problems (a `{{ }}` the step can't read) show under the preview.
 */
export function MarkdownTemplateField({
  label,
  value,
  onChange,
  scope,
  check,
  description,
  placeholder,
  issues = [],
  field,
  writer,
  editorTitle,
  editorDescription,
  ai,
}: {
  label: string
  value: string
  onChange: (value: string) => void
  /** What the step can read, from the builder. */
  scope: Scope
  check?: FieldCheck
  description?: React.ReactNode
  placeholder?: string
  /** The step's issues about this setting beyond the template's own. */
  issues?: Pick<BuilderIssue, "id" | "level" | "message">[]
  /** Its key, for finding it. */
  field?: string
  /** Writes it with AI: under the label into the setting, in the card into the draft. */
  writer?: TemplateWriter
  /** The card's title; the label by default. */
  editorTitle?: React.ReactNode
  editorDescription?: React.ReactNode
  /** The editor's AI: rewriting a selection, writing at the caret. */
  ai?: TemplateAI
}) {
  const readOnly = React.useContext(FieldsReadOnly)
  const key = React.useId()
  const labelId = React.useId()
  const card = useCompanionCard(key)
  const [draft, setDraft] = React.useState(value)

  const problems = React.useMemo(
    () => (value.trim() ? checkField(value, "template", scope, check) : []),
    [value, scope, check]
  )
  const draftProblems = React.useMemo(
    () => (draft.trim() ? checkField(draft, "template", scope, check) : []),
    [draft, scope, check]
  )
  const invalid =
    problems.some((p) => p.severity === "error") ||
    issues.some((i) => i.level === "error")

  const open = () => {
    if (!card.canOpen) return
    setDraft(value)
    card.show()
  }
  const save = () => {
    if (draft !== value) onChange(draft)
    card.hide()
  }

  return (
    <div
      data-slot="field"
      data-field={field}
      data-invalid={invalid || undefined}
      className="flex flex-col gap-1.5"
    >
      <div className="flex min-h-6 items-center justify-between gap-2">
        <FieldLabel id={labelId}>{label}</FieldLabel>
        <div className="flex items-center gap-1">
          {!readOnly && writer?.({ value, onApply: onChange })}
          <Button
            type="button"
            variant="outline"
            size="xs"
            aria-expanded={card.open}
            aria-describedby={labelId}
            disabled={!card.canOpen}
            onClick={open}
          >
            <Icon icon="file" data-icon="inline-start" />
            {readOnly ? "Open" : "Open editor"}
          </Button>
        </div>
      </div>
      <Preview
        value={value}
        label={label}
        placeholder={placeholder}
        onOpen={open}
      />
      <div aria-live="polite" className="flex flex-col gap-1">
        <IssueMessages issues={issues} />
        <Problems problems={problems} />
        {!problems.length && !issues.length && description && (
          <FieldDescription>{description}</FieldDescription>
        )}
      </div>

      {/* Gone while open (its step's settings changed), it closes too. */}
      <SettingsCompanion
        id={key}
        size="wide"
        icon="file"
        title={editorTitle ?? label}
        description={editorDescription}
        closeLabel="Close without saving"
        bodyClassName="flex flex-col gap-3 px-5 py-4"
        footer={
          <>
            <span className="flex items-center gap-1.5 text-2xs text-subtle max-[600px]:hidden">
              <Icon icon="file" size={14} />
              {readOnly
                ? "Read-only"
                : draft !== value
                  ? "Not saved yet"
                  : "Nothing changes until you save"}
            </span>
            {readOnly ? (
              <Button
                type="button"
                size="sm"
                className="ml-auto"
                onClick={card.hide}
              >
                Done
              </Button>
            ) : (
              <>
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  className="ml-auto"
                  onClick={card.hide}
                >
                  Cancel
                </Button>
                {/* Not a form: the editor's AI prompt is one of its own. */}
                <Button type="button" size="sm" onClick={save}>
                  Save {label.toLowerCase()}
                </Button>
              </>
            )}
          </>
        }
      >
        <React.Suspense
          fallback={
            <div aria-busy="true" className="flex flex-col gap-2">
              <Skeleton className="h-8 w-full" />
              <Skeleton className="h-80 w-full" />
            </div>
          }
        >
          <TemplateEditor
            value={draft}
            onChange={setDraft}
            scope={scope}
            check={check}
            label={label}
            placeholder={placeholder}
            readOnly={readOnly}
            writer={writer}
            ai={ai}
          />
        </React.Suspense>
        {draftProblems.length > 0 && (
          <div aria-live="polite" className="flex flex-col gap-1">
            <Problems problems={draftProblems} />
          </div>
        )}
      </SettingsCompanion>
    </div>
  )
}
