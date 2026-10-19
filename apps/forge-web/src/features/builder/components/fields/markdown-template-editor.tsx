import * as React from "react"
import type { Editor } from "@tiptap/core"
// Brings in the Markdown extension's `editor.markdown` type.
import type {} from "@tiptap/markdown"

import type {
  AIActionDefinition,
  AIResultMode,
  EditorAction,
  EditorAIConfig,
  EditorFeatures,
  EditorRange,
  RichTextEditorRef,
  ToolbarItem,
} from "@/components/forge/rich-text-editor"
import { RichTextEditor } from "@/components/forge/rich-text-editor/rich-text-editor"
import { Button } from "@/components/ui/button"
import type { FieldCheck } from "@/features/steps/lib/expressions"
import type { Scope } from "@/features/steps/lib/scope"
import { ExpressionField } from "./expression-field"
import { TemplateReference, type ReferenceRequest } from "./template-reference"
import "./markdown-template-field.css"

/*
 * The rich text side of a Markdown template setting (markdown-template-field):
 * the read-only preview and the editor, loaded when one first shows. A
 * `{{ expression }}` is a chip; `{{`, `/field` or Insert field opens a
 * field below the text that completes what the step can read. Given `ai`,
 * the editor's AI rewrites a selection or writes at the caret, from the
 * selection menu, the right-click menu, `/` and the toolbar's AI menu.
 */

/** What an instruction can hold: prose formatting, lists, tables, code. */
const FEATURES: EditorFeatures = {
  taskList: true,
  tables: true,
  slashCommands: true,
  characterCount: true,
  // `$` is JSONata's, and typed punctuation should reach the model as typed.
  math: false,
  typography: false,
  emoji: false,
  images: false,
  highlight: false,
}

const PREVIEW_FEATURES: EditorFeatures = {
  ...FEATURES,
  slashCommands: false,
  characterCount: false,
}

const PREVIEW_EXTENSIONS = [TemplateReference]

const TOOLBAR: ToolbarItem[] = [
  "undo",
  "redo",
  "separator",
  "heading",
  "separator",
  "bold",
  "italic",
  "strike",
  "inlineCode",
  "link",
  "clearFormatting",
  "separator",
  "bulletList",
  "orderedList",
  "taskList",
  "indent",
  "outdent",
  "separator",
  "insertField",
  "insert",
  "ai",
  "spacer",
  "more",
]

/** What the AI is asked for one action, as the admin API's instruction editor reads it. */
export type TemplateAIRequest = {
  /** The action's id (`edit`, `insert`, …). */
  action: string
  /** What to do: the action's prompt, then what the person typed. */
  ask: string
  /** The whole setting, in Markdown. */
  document: string
  /** The selected passage, in Markdown; empty at a caret. */
  selection: string
  /** The Markdown just before and after the selection or caret. */
  before: string
  after: string
  /** Where the answer goes. */
  place: AIResultMode
}

/** Runs an AI action on the setting, answering with the Markdown for its place. */
export type TemplateAI = (
  request: TemplateAIRequest,
  signal: AbortSignal
) => Promise<string>

/**
 * The AI in a setting's editor. The two that ask take what the person
 * types: rewriting the selection as they describe, or writing what they
 * describe at the caret.
 */
const AI_ACTIONS: AIActionDefinition[] = [
  {
    id: "edit",
    label: "Rewrite with AI…",
    description: "Change the selection as you describe",
    prompt: "Rewrite the selected passage as the person asks.",
    requiresSelection: true,
    resultMode: "replace",
    askInstruction: true,
    placeholder: "How should it change? Friendlier, shorter, with an example…",
  },
  {
    id: "insert",
    label: "Insert with AI…",
    description: "Write what you describe at the caret",
    prompt: "Write new text for where the caret is, as the person asks.",
    resultMode: "insert",
    askInstruction: true,
    placeholder: "What should go here? A section on refunds, a rule about…",
  },
  {
    id: "improve",
    label: "Improve writing",
    description: "Clearer and more specific for the agent",
    prompt:
      "Rewrite the selected passage so the agent can follow it more easily: clearer, more specific and better organized. Keep its meaning.",
    requiresSelection: true,
    resultMode: "replace",
  },
  {
    id: "shorten",
    label: "Make it shorter",
    description: "Cut it down, keeping what the agent needs",
    prompt:
      "Make the selected passage shorter without losing anything the agent needs.",
    requiresSelection: true,
    resultMode: "replace",
  },
  {
    id: "fix",
    label: "Fix spelling and grammar",
    description: "Correct mistakes without changing the wording",
    prompt:
      "Fix spelling, grammar and punctuation in the selected passage. Change nothing else.",
    requiresSelection: true,
    resultMode: "replace",
  },
  {
    id: "continue",
    label: "Continue writing",
    description: "Write what comes next at the caret",
    prompt: "Write what comes next at the caret, in the instruction's voice.",
    resultMode: "insert",
  },
]

const AI_PLACES = new Map(
  AI_ACTIONS.map((action) => [action.id, action.resultMode ?? "replace"])
)

/** The most of the text around the caret sent each way, as the admin API takes it. */
const AROUND_CHARS = 8_000

/** The Markdown before and after a range, for an answer that goes there. */
function around(editor: Editor, { from, to }: EditorRange) {
  const { doc } = editor.state
  const markdown = (start: number, end: number) =>
    editor.markdown?.serialize({
      type: "doc",
      content: doc.slice(start, end).content.toJSON() ?? [],
    }) ?? ""
  return {
    before: markdown(0, from).slice(-AROUND_CHARS),
    after: markdown(to, doc.content.size).slice(0, AROUND_CHARS),
  }
}

/** The setting's Markdown in the editor's typography, read-only. */
export function TemplatePreview({
  value,
  label,
}: {
  value: string
  label: string
}) {
  return (
    <RichTextEditor
      value={value}
      readOnly
      headless
      bare
      features={PREVIEW_FEATURES}
      extensions={PREVIEW_EXTENSIONS}
      aria-label={label}
    />
  )
}

/** Writes the draft with AI (the instruction writer's ✨), given where it goes. */
export type TemplateWriter = (bind: {
  value: string
  onApply: (value: string) => void
}) => React.ReactNode

/** The writer, drawn where it goes. */
function Writer({
  writer,
  ...bind
}: {
  writer: TemplateWriter
  value: string
  onApply: (value: string) => void
}) {
  return <>{writer(bind)}</>
}

/**
 * The editor in the setting's card: Markdown in and out, with the run's data
 * as `{{ }}` chips. Clicking a chip (or Enter on one) edits it; emptied, it
 * goes.
 */
export function TemplateEditor({
  value,
  onChange,
  scope,
  check,
  label,
  placeholder,
  readOnly,
  writer,
  ai,
}: {
  value: string
  onChange: (value: string) => void
  scope: Scope
  check?: FieldCheck
  label: string
  placeholder?: string
  readOnly: boolean
  writer?: TemplateWriter
  /** Turns the editor's AI on. */
  ai?: TemplateAI
}) {
  const editorRef = React.useRef<RichTextEditorRef>(null)
  const aiConfig = React.useMemo<EditorAIConfig | undefined>(
    () =>
      ai && !readOnly
        ? {
            enabled: true,
            requireApproval: true,
            allowCustomPrompts: false,
            actions: AI_ACTIONS,
            execute: async (request, { signal }) => {
              const editor = editorRef.current?.getTiptapEditor()
              const markdown = await ai(
                {
                  action: request.actionId,
                  ask: request.instruction ?? "Improve it.",
                  document: request.documentMarkdown,
                  selection: request.selectedMarkdown,
                  ...(editor
                    ? around(editor, request.selection)
                    : { before: "", after: "" }),
                  place: AI_PLACES.get(request.actionId) ?? "replace",
                },
                signal
              )
              return { markdown }
            },
          }
        : undefined,
    [ai, readOnly]
  )
  const [picking, setPicking] = React.useState<ReferenceRequest | null>(null)
  const [expression, setExpression] = React.useState("")

  const pick = React.useCallback((request: ReferenceRequest) => {
    setPicking(request)
    setExpression(request.expression?.trim() ?? "")
  }, [])
  const extensions = React.useMemo(
    () => [TemplateReference.configure({ onPick: pick })],
    [pick]
  )
  const actions = React.useMemo<EditorAction[]>(
    () => [
      {
        id: "insertField",
        label: "Insert field",
        icon: "code",
        group: "Insert",
        description: "Put in the run's data, as {{ }}",
        keywords: ["field", "data", "variable", "template", "reference"],
        slashCommand: true,
        execute: (context) => pick({ pos: context.selection.to }),
      },
    ],
    [pick]
  )

  // Through the editor, so a written draft is one step of its undo.
  const applyDraft = (next: string) => {
    if (!editorRef.current?.setMarkdown(next)) onChange(next)
  }

  const editing = picking?.expression !== undefined
  // Back to the text before the field goes: removed while focused, the
  // dialog around would take focus itself.
  const focusText = () => editorRef.current?.getTiptapEditor()?.view.focus()
  const close = () => {
    focusText()
    setPicking(null)
  }
  const apply = (text = expression.trim()) => {
    const editor = editorRef.current?.getTiptapEditor()
    if (!editor || !picking) return
    const pos = Math.min(picking.pos, editor.state.doc.content.size)
    const attrs = { expression: ` ${text} ` }
    if (editing) {
      const node = editor.state.doc.nodeAt(pos)
      if (node?.type.name === "templateReference") {
        editor
          .chain()
          .focus()
          .command(({ tr }) => {
            if (text) tr.setNodeMarkup(pos, undefined, attrs)
            else tr.delete(pos, pos + node.nodeSize)
            return true
          })
          .run()
      }
    } else if (text) {
      editor
        .chain()
        .focus()
        .insertContentAt(pos, { type: "templateReference", attrs })
        .run()
    }
    focusText()
    setPicking(null)
  }

  return (
    // The editor takes the card's height; on a short screen the card scrolls.
    <div className="flex min-h-[26rem] flex-1 flex-col gap-3">
      <div className="flex min-h-6 items-center gap-2">
        <p className="text-2xs text-subtle">
          Markdown, sent as written. Type{" "}
          <code className="font-mono">{"{{"}</code> or{" "}
          <code className="font-mono">/field</code> to put in the run&apos;s
          data.
        </p>
        {!readOnly && writer && (
          <span className="ml-auto">
            <Writer writer={writer} value={value} onApply={applyDraft} />
          </span>
        )}
      </div>
      <RichTextEditor
        ref={editorRef}
        value={value}
        onChange={onChange}
        readOnly={readOnly}
        autoFocus={readOnly ? false : "end"}
        features={FEATURES}
        extensions={extensions}
        actions={actions}
        ai={aiConfig}
        toolbar={{ items: TOOLBAR }}
        placeholder={placeholder}
        aria-label={label}
        className="min-h-0 flex-1"
      />
      {picking && (
        <div
          className="flex flex-col gap-2.5 rounded-(--radius-card) border bg-muted/40 p-3"
          onKeyDownCapture={(event) => {
            if (event.key !== "Enter" && event.key !== "Escape") return
            // An open list of completions takes its keys first.
            if (document.querySelector(".cm-tooltip-autocomplete")) return
            event.preventDefault()
            event.stopPropagation()
            if (event.key === "Enter") apply()
            else close()
          }}
        >
          <ExpressionField
            label={editing ? "Edit the field" : "Insert a field"}
            value={expression}
            onChange={setExpression}
            mode="expression"
            scope={scope}
            check={check}
            autoFocus
            placeholder="input.…, steps.… or state.…"
            description="Its path in the run's data; the list completes it. Enter puts it in."
          />
          <div className="flex items-center gap-2">
            {editing && (
              <Button
                type="button"
                variant="ghost"
                size="sm"
                className="text-destructive hover:text-destructive"
                onClick={() => apply("")}
              >
                Remove
              </Button>
            )}
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="ml-auto"
              onClick={close}
            >
              Cancel
            </Button>
            <Button
              type="button"
              size="sm"
              disabled={!expression.trim()}
              onClick={() => apply()}
            >
              {editing ? "Save field" : "Insert"}
            </Button>
          </div>
        </div>
      )}
    </div>
  )
}
