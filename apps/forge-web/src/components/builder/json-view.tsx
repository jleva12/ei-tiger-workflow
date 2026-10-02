import * as React from "react"

import { CopyButton, JsonView } from "@/components/json/json-view"
import { Icon } from "@/components/forge/icon"
import { Button } from "@/components/ui/button"
import { issueSummary } from "@/lib/builder/types"
import { IssueList } from "./issue-list"
import { useBuilder } from "./store"
import { capitalized, useBuilderUi, withArticle } from "./ui"
import { downloadJson, useBuilderDocument, useOpenStep } from "./utils"

/**
 * The JSON view: the graph document as it's saved and exported, updating
 * as the canvas changes, beside what it holds, what stops it from running,
 * and the JSON Schema of the format.
 */
export function BuilderJson<Doc extends { name: string; nodes: unknown[]; edges: unknown[] }>({
  format,
  schema,
  schemaFileName,
  fileName,
  about,
  onImport,
}: {
  /** The format's name, e.g. forge.agent/v1. */
  format: string
  /** Its JSON Schema. */
  schema: unknown
  schemaFileName: string
  /** The file a document downloads as. */
  fileName: (doc: Doc) => string
  /** What the document's parts are. */
  about: React.ReactNode
  onImport: () => void
}) {
  const doc = useBuilderDocument<Doc>()
  const { nouns, ready } = useBuilderUi()
  const issues = useBuilder((s) => s.issues)
  const openStep = useOpenStep()
  const text = React.useMemo(() => JSON.stringify(doc, null, 2), [doc])
  const lines = text.split("\n").length
  const summary = issueSummary(issues)

  return (
    <div className="grid size-full min-h-0 grid-cols-[minmax(0,1fr)_18rem] @max-[900px]/shell:grid-cols-1 @max-[900px]/shell:grid-rows-[minmax(0,1fr)_auto] @min-[1700px]/shell:grid-cols-[minmax(0,1fr)_20rem]">
      <div className="flex min-h-0 flex-col gap-2.5 p-5 @max-[600px]/shell:p-3">
        <div className="flex items-center gap-2 text-2xs text-muted-foreground">
          <code className="font-mono text-foreground">{fileName(doc)}</code>
          <span aria-hidden="true">·</span>
          <span className="tabular-nums">
            {lines.toLocaleString()} lines, {doc.nodes.length} nodes, {doc.edges.length} edges
          </span>
        </div>
        <JsonView value={doc} className="min-h-0 flex-1" />
      </div>

      <aside
        aria-label="About this JSON"
        className="min-h-0 overflow-y-auto border-l @max-[900px]/shell:max-h-72 @max-[900px]/shell:border-t @max-[900px]/shell:border-l-0"
      >
        <section className="flex flex-col gap-3 px-4 py-4">
          <h2 className="text-sm font-medium text-foreground">{capitalized(nouns.doc)} JSON</h2>
          <p className="text-xs/[1.6] text-muted-foreground">{about}</p>
          <div className="flex gap-2">
            <CopyButton value={text} label="Copy JSON" />
            <Button variant="outline" size="sm" onClick={() => downloadJson(doc, fileName(doc))}>
              <Icon icon="download" data-icon="inline-start" />
              Download
            </Button>
          </div>
        </section>
        <section className="flex flex-col gap-2.5 border-t px-4 py-4">
          <h3 className="text-xs font-medium text-foreground">
            {summary ? `Before it can run: ${summary}` : "Ready to run"}
          </h3>
          {issues.length ? (
            <IssueList issues={issues} onOpen={openStep} />
          ) : (
            <p className="flex items-start gap-2 text-xs/[1.6] text-muted-foreground">
              <Icon icon="completed" size={14} className="mt-0.5 shrink-0 text-signal-success" />
              {ready}
            </p>
          )}
        </section>
        <section className="flex flex-col gap-3 border-t px-4 py-4">
          <h3 className="text-xs font-medium text-foreground">Format</h3>
          <p className="text-xs/[1.6] text-muted-foreground">
            <code className="font-mono text-foreground">{format}</code>, described
            by a JSON Schema (draft 2020-12) a runner or an API can check {nouns.docs} against.
          </p>
          <div className="flex gap-2">
            <CopyButton
              value={JSON.stringify(schema, null, 2)}
              label="Copy schema"
            />
            <Button
              variant="outline"
              size="sm"
              onClick={() => downloadJson(schema, schemaFileName)}
            >
              <Icon icon="download" data-icon="inline-start" />
              Download
            </Button>
          </div>
        </section>
        <section className="flex flex-col gap-3 border-t px-4 py-4">
          <h3 className="text-xs font-medium text-foreground">Replace from JSON</h3>
          <p className="text-xs/[1.6] text-muted-foreground">
            Paste or choose {withArticle(nouns.doc)} file to put its {nouns.steps} on this canvas. You
            can undo it.
          </p>
          <Button variant="outline" size="sm" className="self-start" onClick={onImport}>
            Import JSON…
          </Button>
        </section>
      </aside>
    </div>
  )
}
