import * as React from "react"
import {
  acceptCompletion,
  autocompletion,
  closeBrackets,
  closeBracketsKeymap,
  completionKeymap,
  completionStatus,
  startCompletion,
  type Completion as CMCompletion,
  type CompletionContext,
} from "@codemirror/autocomplete"
import { defaultKeymap, history, historyKeymap } from "@codemirror/commands"
import {
  bracketMatching,
  HighlightStyle,
  StreamLanguage,
  syntaxHighlighting,
} from "@codemirror/language"
import { forceLinting, linter } from "@codemirror/lint"
import { EditorState, Prec, type Extension } from "@codemirror/state"
import {
  Decoration,
  drawSelection,
  EditorView,
  hoverTooltip,
  keymap,
  MatchDecorator,
  placeholder as placeholderText,
  tooltips,
  ViewPlugin,
  type DecorationSet,
  type ViewUpdate,
} from "@codemirror/view"
import { tags } from "@lezer/highlight"
import { cn } from "cn"

import { Icon } from "@/components/forge/icon"
import { resolveIcon } from "@/components/forge/icons"
import { IssueMessages } from "@/features/builder/components/fields/field-issues"
import { FieldDescription, FieldLabel } from "@/components/ui/field"
import type { BuilderIssue } from "@/features/builder/lib/types"
import {
  checkField,
  completeField,
  typeAt,
  type Completion,
  type FieldCheck,
  type FieldMode,
} from "@/features/steps/lib/expressions"
import { normalizeReferences } from "@/features/steps/lib/references"
import type { Scope, StepMark } from "@/features/steps/lib/scope"
import "./expression-field.css"

/* -------------------------------------------------------------------------- */
/* Look                                                                       */
/* -------------------------------------------------------------------------- */

const highlight = HighlightStyle.define([
  {
    tag: [tags.string, tags.special(tags.string)],
    color: "var(--tone-green-foreground)",
  },
  { tag: [tags.number], color: "var(--tone-amber-foreground)" },
  {
    tag: [tags.bool, tags.null, tags.atom],
    color: "var(--tone-pink-foreground)",
  },
  {
    tag: [
      tags.keyword,
      tags.controlKeyword,
      tags.operator,
      tags.definitionKeyword,
    ],
    color: "var(--muted-foreground)",
  },
  { tag: [tags.propertyName, tags.variableName], color: "var(--foreground)" },
  {
    tag: [tags.comment, tags.lineComment, tags.blockComment],
    color: "var(--subtle)",
  },
  { tag: [tags.punctuation, tags.bracket], color: "var(--muted-foreground)" },
])

// JSONata, coloured the way the JSON view colours values.
const WORDS = new Set(["and", "or", "in", "function"])
const ATOMS = new Set(["true", "false", "null"])
const jsonata = StreamLanguage.define<{ comment: boolean }>({
  name: "jsonata",
  startState: () => ({ comment: false }),
  token(stream, state) {
    if (state.comment) {
      if (stream.skipTo("*/")) {
        stream.pos += 2
        state.comment = false
      } else stream.skipToEnd()
      return "comment"
    }
    if (stream.eatSpace()) return null
    if (stream.match("/*")) {
      state.comment = true
      return "comment"
    }
    const c = stream.peek()
    if (c === '"' || c === "'") {
      stream.next()
      let escaped = false
      for (let next = stream.next(); next != null; next = stream.next()) {
        if (next === c && !escaped) break
        escaped = !escaped && next === "\\"
      }
      return "string"
    }
    if (c === "`") {
      stream.next()
      if (stream.skipTo("`")) stream.next()
      else stream.skipToEnd()
      return "propertyName"
    }
    if (stream.match(/^\d+(\.\d+)?([eE][+-]?\d+)?/)) return "number"
    if (stream.match(/^\$\$?\w*/)) return "variableName"
    if (stream.match(/^[A-Za-z_]\w*/)) {
      const word = stream.current()
      return WORDS.has(word)
        ? "keyword"
        : ATOMS.has(word)
          ? "atom"
          : "propertyName"
    }
    if (
      stream.match(/^(:=|~>|!=|<=|>=|\.\.)/) ||
      stream.match(/^[=<>&+\-*/%?:!]/)
    )
      return "operator"
    if (stream.match(/^[()[\]{}]/)) return "bracket"
    stream.next()
    return "punctuation"
  },
  languageData: { commentTokens: { block: { open: "/*", close: "*/" } } },
})

// Completions, hovered types and problems, on the menu surface.
const floating = EditorView.theme({
  ".cm-tooltip": {
    border: "1px solid var(--border)",
    borderRadius: "var(--radius-band)",
    backgroundColor: "var(--popover)",
    color: "var(--foreground)",
    boxShadow: "var(--shadow-float)",
    fontFamily: "var(--font-sans)",
    zIndex: "60",
  },
  ".cm-tooltip.cm-tooltip-autocomplete": { padding: "0.25rem" },
  ".cm-tooltip.cm-tooltip-autocomplete > ul": {
    fontFamily: "var(--font-sans)",
    maxHeight: "16rem",
    minWidth: "15rem",
    maxWidth: "28rem",
  },
  ".cm-tooltip.cm-tooltip-autocomplete > ul > li": {
    display: "flex",
    alignItems: "baseline",
    gap: "0.75rem",
    borderRadius: "var(--radius-item)",
    padding: "0.3125rem 0.5rem",
    color: "var(--foreground)",
    lineHeight: "1.4",
  },
  ".cm-tooltip.cm-tooltip-autocomplete > ul > li[aria-selected]": {
    backgroundColor: "var(--muted)",
    color: "var(--foreground)",
  },
  ".cm-completionLabel": {
    fontFamily: "var(--font-mono)",
    fontSize: "0.75rem",
  },
  ".cm-completionNote": {
    minWidth: "0",
    overflow: "hidden",
    textOverflow: "ellipsis",
    whiteSpace: "nowrap",
    fontSize: "0.75rem",
    color: "var(--muted-foreground)",
  },
  // A step's mark: its glyph on a small tile, or an agent's orb, as on the canvas.
  ".cm-completionMark": {
    alignSelf: "center",
    display: "grid",
    placeItems: "center",
    flexShrink: "0",
    width: "1.125rem",
    height: "1.125rem",
    borderRadius: "var(--radius-chip)",
    border: "1px solid var(--border)",
    backgroundColor: "var(--background)",
    color: "var(--muted-foreground)",
  },
  ".cm-completionMark[data-mark=orb]": {
    width: "0.875rem",
    height: "0.875rem",
    margin: "0 0.125rem",
    borderRadius: "9999px",
    border: "0",
  },
  ".cm-completionMark[data-mark=ink]": {
    borderColor: "transparent",
    backgroundColor: "var(--primary)",
    color: "var(--primary-foreground)",
  },
  ".cm-completionMark[data-mark=person]": {
    borderColor: "var(--notice-border)",
    color: "var(--notice-accent)",
  },
  ".cm-completionMark svg": { width: "0.6875rem", height: "0.6875rem" },
  ".cm-completionMatchedText": { textDecoration: "none", fontWeight: "600" },
  ".cm-completionDetail": {
    marginLeft: "auto",
    maxWidth: "14rem",
    overflow: "hidden",
    textOverflow: "ellipsis",
    whiteSpace: "nowrap",
    fontFamily: "var(--font-mono)",
    fontSize: "0.6875rem",
    fontStyle: "normal",
    color: "var(--muted-foreground)",
  },
  ".cm-tooltip.cm-completionInfo": {
    maxWidth: "18rem",
    padding: "0.5rem 0.625rem",
    fontSize: "0.75rem",
    lineHeight: "1.5",
    color: "var(--muted-foreground)",
  },
  ".cm-tooltip.cm-tooltip-hover": {
    padding: "0.5rem 0.625rem",
    maxWidth: "20rem",
  },
  ".cm-tooltip.cm-tooltip-lint": { padding: "0.25rem", maxWidth: "22rem" },
  ".cm-diagnostic": {
    border: "0",
    padding: "0.375rem 0.5rem",
    fontSize: "0.75rem",
    lineHeight: "1.5",
  },
  ".cm-diagnostic-error": { color: "var(--destructive)" },
  ".cm-diagnostic-warning": { color: "var(--tone-amber-foreground)" },
  ".cm-tooltip-arrow": { display: "none" },
})

// `{{ … }}` in text reads as a chip.
const templateChips = ViewPlugin.fromClass(
  class {
    decorations: DecorationSet
    matcher = new MatchDecorator({
      regexp: /\{\{[\s\S]*?\}\}/g,
      decoration: Decoration.mark({ class: "cm-template" }),
    })
    constructor(view: EditorView) {
      this.decorations = this.matcher.createDeco(view)
    }
    update(update: ViewUpdate) {
      this.decorations = this.matcher.updateDeco(update, this.decorations)
    }
  },
  { decorations: (plugin) => plugin.decorations }
)

// One line only: a pasted or typed newline becomes a space.
const oneLine = EditorState.transactionFilter.of((tr) =>
  tr.docChanged && tr.newDoc.lines > 1
    ? [
        tr,
        {
          changes: {
            from: 0,
            to: tr.newDoc.length,
            insert: tr.newDoc.toString().replace(/\s*\n\s*/g, " "),
          },
          sequential: true,
        },
      ]
    : tr
)

/** A completion carrying Forge's extras for rendering. */
type Marked = CMCompletion & { forge?: Pick<Completion, "step" | "note"> }

const SVG = "http://www.w3.org/2000/svg"

/**
 * A step's mark for the completion list (plain DOM, which CodeMirror
 * renders): an agent's orb, or its kind's glyph on a hairline, ink or
 * notice tile, the way StepGlyph draws it on the canvas.
 */
function stepMark(info: StepMark): HTMLElement {
  const dom = document.createElement("span")
  dom.className = "cm-completionMark"
  dom.setAttribute("aria-hidden", "true")
  if (info.orb !== undefined) {
    dom.dataset.mark = "orb"
    dom.classList.add(`orb-agent-${info.orb}`)
    return dom
  }
  dom.dataset.mark = info.terminal ? "ink" : info.person ? "person" : "tile"
  const svg = document.createElementNS(SVG, "svg")
  svg.setAttribute("viewBox", "0 0 24 24")
  svg.setAttribute("fill", "none")
  svg.setAttribute("stroke-width", "1.8")
  for (const [tag, attributes] of resolveIcon(info.icon)) {
    const part = document.createElementNS(SVG, tag)
    for (const [name, value] of Object.entries(attributes)) {
      if (name === "key") continue
      part.setAttribute(
        name.replace(/[A-Z]/g, (c) => `-${c.toLowerCase()}`),
        String(value)
      )
    }
    svg.appendChild(part)
  }
  dom.appendChild(svg)
  return dom
}

const COMPLETION_TYPE: Record<Completion["kind"], string> = {
  variable: "variable",
  property: "property",
  method: "method",
  value: "enum",
  argument: "property",
}

/* -------------------------------------------------------------------------- */
/* Field                                                                      */
/* -------------------------------------------------------------------------- */

type Props = {
  label: string
  value: string
  onChange: (value: string) => void
  mode: FieldMode
  /** What the step can read, from the builder. */
  scope: Scope
  check?: FieldCheck
  description?: React.ReactNode
  placeholder?: string
  /** More than one line (prose, arguments, a transform). */
  multiline?: boolean
  /** Its height before it grows, in lines. */
  rows?: number
  /** Show only the label to assistive technology (a list row's field). */
  labelHidden?: boolean
  className?: string
  /**
   * The step's issues about this setting beyond what the field checks
   * itself (it's empty, a case it can't be), shown with its own; and its
   * key, for finding it.
   */
  issues?: Pick<BuilderIssue, "id" | "level" | "message">[]
  field?: string
}

/**
 * A setting that reads the run's data, in JSONata, in an editor that knows
 * what the step can read: it completes paths (`steps.` offers the steps
 * before this one; `output.` offers that output's fields, with their
 * types; after `= "` an allowed value; `$` JSONata's functions), shows a
 * path's type on hover, and underlines what doesn't fit, saying why under
 * the field. In text, `{{` opens an expression. Tab and Enter take a
 * completion; Escape closes the list.
 */
export function ExpressionField({
  label,
  value,
  onChange,
  mode,
  scope,
  check,
  description,
  placeholder,
  multiline = false,
  rows,
  labelHidden,
  className,
  issues = [],
  field,
}: Props) {
  const host = React.useRef<HTMLDivElement>(null)
  const view = React.useRef<EditorView | null>(null)
  const labelId = React.useId()
  const messagesId = React.useId()
  // The editor reads these when it runs, not when it's made.
  const latest = React.useRef({ onChange, scope, check, mode })
  React.useEffect(() => {
    latest.current = { onChange, scope, check, mode }
  })

  // Where the cursor is while the field has focus, and whether completions are open:
  // what's being typed isn't judged yet, and the list mustn't cover the messages.
  const [editing, setEditing] = React.useState<{
    caret: number | null
    completing: boolean
  }>({
    caret: null,
    completing: false,
  })
  const settled = (d: { from: number; to: number }, caret: number | null) =>
    caret === null || caret < d.from || caret > d.to
  const problems = React.useMemo(
    () =>
      (value.trim() ? checkField(value, mode, scope, check) : []).filter((d) =>
        settled(d, editing.caret)
      ),
    [value, mode, scope, check, editing.caret]
  )
  const errors = problems.filter((p) => p.severity === "error")
  const shown = [
    ...errors,
    ...problems.filter((p) => p.severity === "warning"),
  ].slice(0, 2)

  React.useEffect(() => {
    if (!host.current) return
    const complete = (context: CompletionContext) => {
      const { scope: now, mode: kind } = latest.current
      const text = context.state.doc.toString()
      const result = completeField(text, context.pos, kind, now)
      if (!result?.options.length) return null
      if (!context.explicit && result.from === context.pos) {
        // Nothing typed yet: open only right after `.`, `$`, `{{`, `= "` or a new argument.
        const before = text.slice(0, context.pos)
        const opens =
          /[^.]\.$/.test(before) ||
          /\$$/.test(before) ||
          /\{\{\s*$/.test(before) ||
          /(!=|=)\s*["']$/.test(before)
        if (!opens) return null
      }
      return {
        from: result.from,
        to: result.to,
        validFor: /^[\w$]*$/,
        options: result.options.map((option): Marked => ({
          forge: { step: option.step, note: option.note },
          label: option.label,
          detail: option.detail,
          info: option.info,
          type: COMPLETION_TYPE[option.kind],
          boost: option.boost,
          apply: (editor, _completion, from, to) => {
            const insert = option.apply ?? option.label
            const start = from - (option.replaceBefore ?? 0)
            // An argument's empty string: the cursor goes between its quotes.
            const cursor = insert.endsWith('""')
              ? start + insert.length - 1
              : start + insert.length + (option.skipAfter ?? 0)
            editor.dispatch({
              changes: { from: start, to, insert },
              selection: { anchor: cursor },
              userEvent: "input.complete",
            })
          },
        })),
      }
    }

    const extensions: Extension[] = [
      history(),
      drawSelection(),
      bracketMatching(),
      // Prose has apostrophes: brackets and quotes close themselves only in code.
      mode === "template" ? [] : closeBrackets(),
      keymap.of([
        { key: "Tab", run: acceptCompletion },
        ...(mode === "template" ? [] : closeBracketsKeymap),
        ...completionKeymap,
        ...historyKeymap,
        ...defaultKeymap,
      ]),
      mode === "template" ? [] : jsonata,
      syntaxHighlighting(highlight),
      mode === "template" ? templateChips : [],
      multiline ? EditorView.lineWrapping : oneLine,
      autocompletion({
        override: [complete],
        icons: false,
        closeOnBlur: true,
        addToOptions: [
          // A step's mark, as on the canvas.
          {
            position: 20,
            render: (completion) => {
              const mark = (completion as Marked).forge?.step
              return mark ? stepMark(mark) : null
            },
          },
          // Its name, or what a root is, in the UI face.
          {
            position: 60,
            render: (completion) => {
              const note = (completion as Marked).forge?.note
              if (!note) return null
              const dom = document.createElement("span")
              dom.className = "cm-completionNote"
              dom.textContent = note
              return dom
            },
          },
        ],
      }),
      linter(
        (editor) => {
          const text = editor.state.doc.toString()
          if (!text.trim()) return []
          const { scope: now, check: options, mode: kind } = latest.current
          const caret = editor.hasFocus
            ? editor.state.selection.main.head
            : null
          return checkField(text, kind, now, options)
            .filter((d) => settled(d, caret))
            .map((d) => {
              const from = Math.min(d.from, Math.max(0, text.length - 1))
              return {
                from,
                to: Math.min(Math.max(d.to, from + 1), text.length),
                severity: d.severity,
                message: d.message,
              }
            })
        },
        { delay: 150 }
      ),
      hoverTooltip(
        (editor, pos) => {
          const hit = typeAt(
            editor.state.doc.toString(),
            pos,
            latest.current.mode,
            latest.current.scope
          )
          if (!hit) return null
          return {
            pos: hit.from,
            end: hit.to,
            above: true,
            create: () => {
              const dom = document.createElement("div")
              const type = dom.appendChild(document.createElement("div"))
              type.className = "font-mono text-xs text-foreground"
              type.textContent = hit.label
              if (hit.description) {
                const about = dom.appendChild(document.createElement("div"))
                about.className = "mt-1 text-xs/[1.5] text-muted-foreground"
                about.textContent = hit.description
              }
              return { dom }
            },
          }
        },
        { hoverTime: 350 }
      ),
      // Float above the dialog's scroll area, not clipped by it.
      tooltips({ parent: document.body }),
      floating,
      placeholder ? placeholderText(placeholder) : [],
      // `{{` in any setting: close it and offer fields before closeBrackets runs.
      Prec.high(
        EditorView.inputHandler.of((editor, from, to, text) => {
          const before = editor.state.sliceDoc(0, from)
          if (text !== "{" || !before.endsWith("{")) return false
          if (
            mode !== "template" &&
            !normalizeReferences(before + text, false).endsWith("( ")
          )
            return false
          // Take in a } the first { closed by itself.
          const end = editor.state.sliceDoc(to, to + 1) === "}" ? to + 1 : to
          editor.dispatch({
            changes: { from, to: end, insert: "{  }}" },
            selection: { anchor: from + 2 },
            userEvent: "input.type",
          })
          startCompletion(editor)
          return true
        })
      ),
      EditorView.contentAttributes.of({
        "aria-labelledby": labelId,
        "aria-describedby": messagesId,
        "aria-multiline": String(multiline),
        spellcheck: mode === "template" ? "true" : "false",
        autocapitalize: "off",
      }),
      EditorView.updateListener.of((update) => {
        if (
          update.docChanged &&
          !update.transactions.some((tr) => tr.isUserEvent("sync"))
        ) {
          latest.current.onChange(update.state.doc.toString())
        }
        if (
          update.docChanged ||
          update.selectionSet ||
          update.focusChanged ||
          update.transactions.length
        ) {
          const caret = update.view.hasFocus
            ? update.state.selection.main.head
            : null
          const completing = completionStatus(update.state) === "active"
          setEditing((now) =>
            now.caret === caret && now.completing === completing
              ? now
              : { caret, completing }
          )
          // Underlines follow the cursor: what it leaves is judged.
          if (
            !update.docChanged &&
            (update.selectionSet || update.focusChanged)
          ) {
            requestAnimationFrame(() => {
              if (!update.view.dom.isConnected) return
              forceLinting(update.view)
            })
          }
        }
      }),
    ]

    const editor = new EditorView({
      parent: host.current,
      state: EditorState.create({ doc: value, extensions }),
    })
    view.current = editor
    return () => {
      editor.destroy()
      view.current = null
    }
    // The editor is made once per mode; later values arrive through the sync below.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mode, multiline])

  // Follow the value when it changes from outside (undo, a rename, import).
  React.useEffect(() => {
    const editor = view.current
    if (!editor || editor.state.doc.toString() === value) return
    editor.dispatch({
      changes: { from: 0, to: editor.state.doc.length, insert: value },
      userEvent: "sync",
    })
  }, [value])

  // Recheck when what the step can read changes.
  React.useEffect(() => {
    if (view.current) forceLinting(view.current)
  }, [scope, check])

  return (
    <div
      data-slot="field"
      data-mode={mode}
      data-field={field}
      data-invalid={
        errors.length || issues.some((i) => i.level === "error")
          ? true
          : undefined
      }
      className={cn("forge-expression flex flex-col gap-1.5", className)}
    >
      <FieldLabel
        id={labelId}
        className={cn(labelHidden && "sr-only")}
        onClick={() => view.current?.focus()}
      >
        {label}
      </FieldLabel>
      <div
        ref={host}
        data-rows={multiline && rows ? rows : undefined}
        style={
          multiline && rows
            ? ({ "--rows": rows } as React.CSSProperties)
            : undefined
        }
      />
      <div
        id={messagesId}
        aria-live="polite"
        className={cn(
          "flex flex-col gap-1",
          editing.completing && shown.length > 0 && "invisible"
        )}
      >
        <IssueMessages issues={issues} />
        {shown.map((problem, index) => (
          <p
            key={`${problem.from}-${index}`}
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
            {problems.length - shown.length} more; hover the underlines to read
            them.
          </p>
        )}
        {!shown.length && !issues.length && description && (
          <FieldDescription>{description}</FieldDescription>
        )}
      </div>
    </div>
  )
}
