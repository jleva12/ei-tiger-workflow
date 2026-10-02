import * as React from "react"

import {
  RichTextEditor,
  RichTextView,
  type MentionSource,
} from "@/components/forge/rich-text-editor"

import { SectionPage, Specimen } from "../specimen"

const people = [
  { id: "ada", label: "Ada Lovelace", description: "Engineering" },
  { id: "grace", label: "Grace Hopper", description: "Platform" },
  { id: "alan", label: "Alan Turing", description: "Research" },
  { id: "katherine", label: "Katherine Johnson", description: "Analytics" },
]

const mentions: MentionSource = (query) =>
  people.filter((person) =>
    person.label.toLowerCase().includes(query.toLowerCase())
  )

const brief = `# Billing redesign

Ship the new **plans page** and migrate existing customers in batches. Owners: @Ada and @Grace.

## Checklist

- [x] Confirm pricing with finance
- [x] Size the pilot batch
- [ ] Draft the announcement
- [ ] Staged rollout

## Rollout

| Batch | Customers | Share |
| --- | ---: | ---: |
| Pilot | 276 | 15% |
| Remainder | 1,564 | 85% |

The pilot is $0.15 \\times 1840 = 276$ accounts.

\`\`\`ts
export const billingRedesign = flag("billing-redesign", { rollout: 0.15 })
\`\`\`

> Type / for blocks, @ to mention someone, and ⌘F to find and replace.`

const saved = `<h3>Release notes</h3><p>Invoices now show <strong>tax per line</strong>, and annual plans can be <em>paused</em> for up to three months.</p><ul><li><p>Faster checkout</p></li><li><p>Clearer receipts</p></li></ul>`

export function RichTextEditorSection() {
  const [markdown, setMarkdown] = React.useState(brief)
  return (
    <SectionPage
      icon="file"
      eyebrow="Workspace"
      title="Rich text editor"
      description="A complete rich text field on Tiptap in the workspace look: a fixed toolbar, selection and table menus, / commands, @ mentions, block handles, find and replace, an outline, full screen, uploads, embeds and KaTeX formulas, with HTML, Markdown or JSON values. It loads lazily, so Tiptap stays out of a page's bundle until an editor renders."
    >
      <Specimen
        title="Full"
        variant="flush"
        description="Every feature group on. The value is Markdown here, kept in sync below the editor as you type."
        code={`<RichTextEditor
  format="markdown"
  value={markdown}
  onChange={setMarkdown}
  mentions={(query) => people.filter((p) => p.label.includes(query))}
  onUpload={async (file) => (await api.upload(file)).url}
/>`}
      >
        <div className="flex flex-col gap-3 p-4">
          <RichTextEditor
            format="markdown"
            value={markdown}
            onChange={setMarkdown}
            mentions={mentions}
            aria-label="Billing redesign brief"
            contentClassName="min-h-[320px] max-h-[520px]"
          />
          <details className="text-xs text-muted-foreground">
            <summary className="cursor-pointer select-none">
              Markdown value ({markdown.length} characters)
            </summary>
            <pre className="mt-2 max-h-48 overflow-auto rounded-(--radius-card) bg-muted p-3 font-mono text-2xs whitespace-pre-wrap">
              {markdown}
            </pre>
          </details>
        </div>
      </Specimen>
      <div className="grid grid-cols-2 gap-6 @max-[1050px]/shell:grid-cols-1">
        <Specimen
          title="Standard"
          description="Headings, lists, links, code and mentions, without the styling, media and table groups: comments and descriptions."
          code={`<RichTextEditor preset="standard" placeholder="Add a comment…" />`}
        >
          <RichTextEditor
            preset="standard"
            placeholder="Add a comment… @ mentions a teammate"
            mentions={mentions}
            aria-label="Comment"
            contentClassName="min-h-[120px]"
          />
        </Specimen>
        <Specimen
          title="Minimal"
          description="Inline formatting only, no toolbar groups or counts."
          code={`<RichTextEditor preset="minimal" maxLength={280} />`}
        >
          <RichTextEditor
            preset="minimal"
            placeholder="A short status…"
            maxLength={280}
            aria-label="Status"
            contentClassName="min-h-[72px]"
          />
        </Specimen>
      </div>
      <Specimen
        title="Read-only view"
        description="RichTextView shows saved content in the editor's typography, without the field frame or toolbar."
        code={`<RichTextView value={html} />`}
      >
        <RichTextView value={saved} aria-label="Release notes" />
      </Specimen>
    </SectionPage>
  )
}
