import * as React from "react"

import { ErrorCallout } from "@/components/forge/feedback"
import { Icon } from "@/components/forge/icon"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Field, FieldLabel } from "@/components/ui/field"
import { Textarea } from "@/components/ui/textarea"
import { capitalized, type BuilderNouns } from "./ui"

/** What reading a document gave: it, with what was left out to fit; or why not. */
export type ImportResult<Doc> =
  | {
      ok: true
      doc: Doc
      /** What was dropped or filled in to make it fit. */
      notes: string[]
      /** Steps had no position, so the builder should tidy them. */
      needsLayout: boolean
    }
  | { ok: false; error: string }

/**
 * Reads a document from pasted JSON or a file. What it had to drop to fit
 * the format is listed before anything changes; the caller decides what
 * importing does (replace the canvas, or add one).
 */
export function ImportDialog<Doc extends { name: string; nodes: unknown[]; edges: unknown[] }>({
  open,
  onOpenChange,
  format,
  nouns,
  parse,
  title,
  description,
  action,
  onImport,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** The format's name, shown in the placeholder. */
  format: string
  nouns: Pick<BuilderNouns, "doc" | "steps">
  /** Reads what was pasted or chosen. */
  parse: (text: string) => ImportResult<Doc>
  title: string
  description: string
  /** The import button's label. */
  action: string
  onImport: (doc: Doc, needsLayout: boolean) => void
}) {
  const [text, setText] = React.useState("")
  const [fileName, setFileName] = React.useState<string>()
  const file = React.useRef<HTMLInputElement>(null)
  const fieldId = React.useId()

  const changeOpen = (next: boolean) => {
    if (!next) {
      setText("")
      setFileName(undefined)
    }
    onOpenChange(next)
  }

  const result = React.useMemo(() => (text.trim() ? parse(text) : null), [text, parse])

  return (
    <Dialog open={open} onOpenChange={changeOpen}>
      <DialogContent className="sm:max-w-lg">
        <form
          noValidate
          className="grid gap-5"
          onSubmit={(event) => {
            event.preventDefault()
            if (result?.ok) {
              onImport(result.doc, result.needsLayout)
              changeOpen(false)
            }
          }}
        >
          <DialogHeader>
            <DialogTitle>{title}</DialogTitle>
            <DialogDescription>{description}</DialogDescription>
          </DialogHeader>
          <Field>
            <div className="flex items-center justify-between gap-2">
              <FieldLabel htmlFor={fieldId}>{capitalized(nouns.doc)} JSON</FieldLabel>
              <Button
                type="button"
                variant="outline"
                size="xs"
                onClick={() => file.current?.click()}
              >
                <Icon icon="file" data-icon="inline-start" />
                Choose a file
              </Button>
            </div>
            <Textarea
              id={fieldId}
              value={text}
              autoFocus
              spellCheck={false}
              placeholder={`{\n  "format": "${format}",\n  "nodes": [ … ],\n  "edges": [ … ]\n}`}
              onChange={(event) => {
                setText(event.target.value)
                setFileName(undefined)
              }}
              className="max-h-64 min-h-40 field-sizing-fixed font-mono text-xs break-all md:text-xs"
            />
            {fileName && (
              <p className="text-2xs text-muted-foreground">
                From <span className="font-mono text-foreground">{fileName}</span>
              </p>
            )}
            <input
              ref={file}
              type="file"
              accept="application/json,.json"
              className="hidden"
              onChange={async (event) => {
                const picked = event.target.files?.[0]
                event.target.value = ""
                if (!picked) return
                setText(await picked.text())
                setFileName(picked.name)
              }}
            />
          </Field>
          {result && !result.ok && (
            <ErrorCallout title="Can't import this">{result.error}</ErrorCallout>
          )}
          {result?.ok && (
            <div className="flex flex-col gap-1.5 rounded-(--radius-card) border px-3.5 py-3 text-xs">
              <p className="flex items-center gap-2 font-medium text-foreground">
                <Icon icon="completed" size={14} className="text-signal-success" />
                {result.doc.name}: {result.doc.nodes.length} {nouns.steps},{" "}
                {result.doc.edges.length} connections
              </p>
              {result.notes.length > 0 && (
                <>
                  <p className="text-muted-foreground">
                    To fit the format, the import leaves out or resets:
                  </p>
                  <ul className="max-h-28 list-disc overflow-y-auto pl-5 font-mono text-2xs/[1.6] text-muted-foreground">
                    {result.notes.map((note, index) => (
                      <li key={index}>{note}</li>
                    ))}
                  </ul>
                </>
              )}
              {result.needsLayout && (
                <p className="text-muted-foreground">
                  Some {nouns.steps} have no position; they'll be tidied.
                </p>
              )}
            </div>
          )}
          <DialogFooter>
            <DialogClose render={<Button type="button" variant="outline" />}>
              Cancel
            </DialogClose>
            <Button type="submit" disabled={!result?.ok}>
              {action}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
