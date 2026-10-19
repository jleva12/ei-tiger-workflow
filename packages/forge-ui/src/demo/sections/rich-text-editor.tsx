import * as React from "react"
import { TextFontIcon } from "@hugeicons/core-free-icons"

import { Button } from "@/components/ui/button"
import {
  RichTextEditor,
  RichTextView,
  type AIRequest,
  type AIStreamEvent,
  type EditorAction,
  type EditorAIConfig,
  type EditorError,
  type MentionSource,
  type RichTextEditorRef,
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

Ship the new **plans page** and migrate existing customers in batches. The current page is confusing and it makes people contact support before they upgrade.

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

> Select text and right-click for AI, type / for blocks, @ to mention someone.`

const wait = (ms: number, signal: AbortSignal) =>
  new Promise<void>((resolve, reject) => {
    const timer = setTimeout(resolve, ms)
    signal.addEventListener("abort", () => {
      clearTimeout(timer)
      reject(new DOMException("Cancelled", "AbortError"))
    })
  })

/** Canned answers, so the demo shows the flow without a model. */
function cannedAnswer(request: AIRequest) {
  const text = request.selectedText.trim()
  switch (request.actionId) {
    case "rewrite":
      return `${text.replace(/\s+/g, " ").replace(/\bit makes\b/, "makes")} *(rewritten for clarity)*`
    case "fix":
      return text.replace(/\bteh\b/g, "the").replace(/\s{2,}/g, " ")
    case "summarize":
      return `**Summary:** ${(text || request.documentMarkdown).split(/(?<=\.)\s/)[0].replace(/^#+\s*/, "")}`
    case "expand":
      return `In more detail: ${text.toLowerCase()} This matters because pricing changes reach every account, so each batch gets a week of monitoring before the next one starts.`
    case "continue":
      return "Once the pilot looks healthy, the remainder follows in two waves, a week apart."
    default:
      return `You asked: *${request.instruction}*. A real model would answer here; this demo echoes the instruction for “${text || "the caret"}”.`
  }
}

/**
 * A stand-in for a real endpoint: streams a canned answer word by word,
 * stopping when the editor cancels.
 */
const simulatedAI: EditorAIConfig = {
  enabled: true,
  requireApproval: true,
  execute: async function* (
    request,
    { signal }
  ): AsyncGenerator<AIStreamEvent> {
    await wait(350, signal)
    for (const word of cannedAnswer(request).split(/(?<= )/)) {
      await wait(45, signal)
      yield { type: "delta", text: word }
    }
    yield { type: "complete" }
  },
}

const uppercase: EditorAction = {
  id: "custom.uppercase",
  label: "Convert to uppercase",
  icon: TextFontIcon,
  shortcut: "Mod-Shift-u",
  isEnabled: (ctx) => ctx.selection.from !== ctx.selection.to,
  execute: (ctx) => {
    ctx.editor.replaceSelection(ctx.selectedText.toUpperCase())
  },
}
const actions = [uppercase]

const saved = `### Release notes

Invoices now show **tax per line**, and annual plans can be *paused* for up to three months.

- Faster checkout
- Clearer receipts`

export function RichTextEditorSection() {
  const [markdown, setMarkdown] = React.useState(brief)
  const editorRef = React.useRef<RichTextEditorRef>(null)
  const [log, setLog] = React.useState<string[]>([])
  const note = (line: string) => setLog((was) => [line, ...was].slice(0, 4))

  return (
    <SectionPage
      icon="file"
      eyebrow="Workspace"
      title="Rich text editor"
      description="A Markdown editor on Tiptap: Markdown in and out, a configurable toolbar, selection, right-click and / menus that all run one action registry, a controller for inserting content from outside, and optional AI from a callback you supply — streamed into a preview and only written in once accepted. It loads lazily, so Tiptap stays out of a page's bundle until an editor renders."
    >
      <Specimen
        title="Everything, with AI"
        variant="flush"
        description="The full preset with a simulated AI (it streams canned answers; no model is called), mentions and a custom action (⌘⇧U, also in the right-click menu). The Markdown value stays in sync below as you type."
        code={`<RichTextEditor
  preset="full"
  value={markdown}
  onChange={setMarkdown}
  actions={[uppercase]}
  mentions={(query) => people.filter((p) => p.label.includes(query))}
  ai={{
    enabled: true,
    execute: (request, { signal }) =>
      fetch("/api/ai/editor", { method: "POST", body: JSON.stringify(request), signal })
        .then((response) => response.json()),
  }}
/>`}
      >
        <div className="flex flex-col gap-3 p-4">
          <RichTextEditor
            ref={editorRef}
            preset="full"
            value={markdown}
            onChange={setMarkdown}
            mentions={mentions}
            actions={actions}
            ai={simulatedAI}
            onError={(error: EditorError) =>
              note(`${error.code}: ${error.message}`)
            }
            aria-label="Billing redesign brief"
            minHeight={320}
            maxHeight={520}
          />
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="mr-1 text-2xs text-muted-foreground">
              From outside, through the ref:
            </span>
            <Button
              size="xs"
              variant="outline"
              onClick={() =>
                editorRef.current?.insertMarkdown("**Inserted** at the caret ")
              }
            >
              insertMarkdown
            </Button>
            <Button
              size="xs"
              variant="outline"
              onClick={() =>
                editorRef.current?.appendMarkdown(
                  "## Appended\n\nA report section added by the app."
                )
              }
            >
              appendMarkdown
            </Button>
            <Button
              size="xs"
              variant="outline"
              onClick={() =>
                editorRef.current?.replaceSelection("~~replaced~~")
              }
            >
              replaceSelection
            </Button>
            <Button
              size="xs"
              variant="outline"
              onClick={() => void editorRef.current?.runAI("rewrite")}
            >
              runAI("rewrite")
            </Button>
            <Button
              size="xs"
              variant="ghost"
              onClick={() => setMarkdown(brief)}
            >
              Reset value
            </Button>
          </div>
          {log.length > 0 && (
            <ul className="flex flex-col gap-0.5 font-mono text-2xs text-muted-foreground">
              {log.map((line, index) => (
                <li key={index}>{line}</li>
              ))}
            </ul>
          )}
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
          title="Configured toolbar"
          description="The standard preset (the default) with the toolbar items listed explicitly, uncontrolled."
          code={`<RichTextEditor
  defaultValue="# New document"
  toolbar={{
    items: ["undo", "redo", "separator", "heading", "bold", "italic",
      "underline", "separator", "bulletList", "orderedList", "link", "codeBlock"],
  }}
/>`}
        >
          <RichTextEditor
            defaultValue={"# New document\n\nStart writing…"}
            toolbar={{
              items: [
                "undo",
                "redo",
                "separator",
                "heading",
                "bold",
                "italic",
                "underline",
                "separator",
                "bulletList",
                "orderedList",
                "link",
                "codeBlock",
              ],
            }}
            aria-label="New document"
            minHeight={140}
          />
        </Specimen>
        <Specimen
          title="Minimal"
          description="Inline formatting, lists and links for a short field, with a length limit."
          code={`<RichTextEditor preset="minimal" maxLength={280} />`}
        >
          <RichTextEditor
            preset="minimal"
            placeholder="A short status…"
            maxLength={280}
            aria-label="Status"
            minHeight={72}
          />
        </Specimen>
      </div>
      <Specimen
        title="Read-only view"
        description="RichTextView shows saved Markdown in the editor's typography, without the field frame, toolbar or menus."
        code={`<RichTextView value={markdown} />`}
      >
        <RichTextView value={saved} aria-label="Release notes" />
      </Specimen>
    </SectionPage>
  )
}
