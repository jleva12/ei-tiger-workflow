import type { Editor } from "@tiptap/core"

import { createBookmark } from "../core/selection-manager.ts"
import type {
  EditorBookmark,
  EditorController,
  EditorError,
  EditorRange,
} from "../types.ts"
import { ASK_AI, DEFAULT_AI_ACTIONS } from "./ai-defaults.ts"
import {
  IDLE_AI_STATE,
  type AIActionDefinition,
  type AIRequest,
  type AIResult,
  type AIResultMode,
  type AIState,
  type AIStreamEvent,
  type EditorAIConfig,
} from "./ai-types.ts"

export { ASK_AI, DEFAULT_AI_ACTIONS }

export interface AIHost {
  controller: EditorController
  getEditor: () => Editor | null
  getConfig: () => EditorAIConfig | undefined
  onError: (error: EditorError) => void
}

export function isAsyncIterable<T>(value: unknown): value is AsyncIterable<T> {
  return (
    typeof value === "object" &&
    value !== null &&
    typeof (value as AsyncIterable<T>)[Symbol.asyncIterator] === "function"
  )
}

// Streamed text reaches the preview at most this often, however fast the
// tokens arrive.
const FLUSH_MS = 60

/**
 * Runs AI actions against the host's `execute` callback: captures the
 * selection as a bookmark, streams the answer into a buffer the preview
 * shows, and on approval writes it where the action said, unless that text
 * has changed meanwhile, in which case the person picks a new place.
 * The document is never touched while an answer streams.
 */
export class AIController {
  private host: AIHost
  private state: AIState = IDLE_AI_STATE
  private listeners = new Set<() => void>()
  private runId = 0
  private abort: AbortController | null = null
  private target: EditorBookmark | null = null
  private request: AIRequest | null = null
  private definition: AIActionDefinition | null = null
  /** What the person typed into the prompt box, to show with the answer. */
  private typed: string | undefined
  private buffer = ""
  private flushTimer: ReturnType<typeof setTimeout> | null = null

  constructor(host: AIHost) {
    this.host = host
  }

  subscribe = (listener: () => void) => {
    this.listeners.add(listener)
    return () => {
      this.listeners.delete(listener)
    }
  }

  getState = () => this.state

  /** The AI commands on offer, `ask` first when custom prompts are allowed. */
  definitions(): AIActionDefinition[] {
    const config = this.host.getConfig()
    if (!config?.enabled) return []
    const actions = config.actions ?? DEFAULT_AI_ACTIONS
    return config.allowCustomPrompts === false ? actions : [ASK_AI, ...actions]
  }

  find(actionId: string) {
    const id = actionId.replace(/^ai\./, "")
    return this.definitions().find((definition) => definition.id === id)
  }

  /**
   * Opens the prompt box for "Ask AI…" (or another action that asks for an
   * instruction), bookmarking the selection then, so the answer lands there
   * even though focus moves to the box.
   */
  openPrompt(range?: EditorRange, actionId: string = ASK_AI.id) {
    const editor = this.host.getEditor()
    const definition = this.find(actionId)
    if (!editor || !definition) return
    this.reset()
    const selection = range ?? this.host.controller.getSelection()
    this.target = createBookmark(editor, selection, { highlight: true })
    this.setState({
      ...IDLE_AI_STATE,
      status: "prompt",
      action: definition,
      hasSelection: selection.from !== selection.to,
    })
  }

  /** Runs the action the prompt box is open for, with what was typed. */
  async submitPrompt(instruction: string) {
    const { status, action } = this.state
    if (status !== "prompt" || !action || !instruction.trim()) return
    await this.start(action.id, { instruction })
  }

  /**
   * Runs `actionId` (`rewrite` or `ai.rewrite`) on `range`, or on the
   * selection. Resolves once the answer is in (or the run ends otherwise).
   */
  async start(
    actionId: string,
    options: { instruction?: string; range?: EditorRange } = {}
  ) {
    const { controller, onError } = this.host
    const editor = this.host.getEditor()
    const definition = this.find(actionId)
    if (!editor || !definition) {
      onError({
        code: "unknown-action",
        message: `There's no AI action "${actionId}".`,
        actionId,
      })
      return
    }

    // From the prompt box, act on what was selected when it opened.
    const prompted =
      this.state.status === "prompt" ? (this.target?.resolve() ?? null) : null
    const range = options.range ?? prompted
    if (range) controller.setSelection(range)
    const selection = controller.getSelection()
    if (definition.requiresSelection && selection.empty) {
      onError({
        code: "action-failed",
        message: `"${definition.label}" needs some selected text.`,
        actionId: `ai.${definition.id}`,
      })
      return
    }

    this.reset()
    this.target = createBookmark(editor, selection, { highlight: true })
    this.definition = definition
    const config = this.host.getConfig()
    const context =
      typeof config?.context === "function"
        ? config.context(definition.id)
        : config?.context
    // What the person typed follows the action's own prompt.
    const typed = options.instruction?.trim() || undefined
    this.typed = typed
    const instruction =
      typed && definition.prompt
        ? `${definition.prompt}\n\n${typed}`
        : (typed ?? definition.prompt)
    this.request = {
      actionId: definition.id,
      ...(instruction && { instruction }),
      selectedText: selection.text,
      selectedMarkdown: controller.getSelectedMarkdown(),
      selection: { from: selection.from, to: selection.to },
      documentMarkdown: controller.getMarkdown(),
      ...(context && { context }),
    }
    await this.run()
  }

  /** Asks again with the same request; the target stays bookmarked. */
  async regenerate() {
    if (!this.request || this.state.status === "running") return
    await this.run()
  }

  /**
   * Writes the answer: where the action said, or `target`. `cursor` puts it
   * at the selection as it is now (the way out of a conflict). Returns
   * whether the document changed.
   */
  accept(target?: AIResultMode | "cursor"): boolean {
    const { status, markdown, mode, hasSelection } = this.state
    if (status !== "ready" && status !== "conflict") return false
    let where = target ?? mode
    if (where === "preview") where = hasSelection ? "replace" : "insert"
    const { controller } = this.host

    let applied: boolean
    if (where === "append") applied = controller.appendMarkdown(markdown)
    else if (where === "cursor") applied = controller.insertMarkdown(markdown)
    else {
      const range = this.target?.resolve() ?? null
      if (!range) {
        this.setState({ ...this.state, status: "conflict" })
        this.host.onError({
          code: "ai-target-changed",
          message:
            "The text this was for changed while the AI worked. Choose where the result goes.",
          actionId: this.definition ? `ai.${this.definition.id}` : undefined,
        })
        return false
      }
      applied =
        where === "replace"
          ? controller.replaceRange(range, markdown)
          : this.insertAfter(range, markdown)
    }
    if (applied) this.reset()
    return applied
  }

  /** Drops the answer; the document stays as it is. */
  reject() {
    this.reset()
  }

  /** Stops a running request and closes the flow. */
  cancel() {
    this.reset()
  }

  dispose() {
    this.reset()
    this.listeners.clear()
  }

  private async run() {
    const config = this.host.getConfig()
    const request = this.request
    if (!config || !request) return
    const runId = (this.runId += 1)
    this.abort?.abort()
    const abort = new AbortController()
    this.abort = abort
    this.buffer = ""
    this.clearFlush()
    const definition = this.definition
    const mode: AIResultMode =
      definition?.resultMode ??
      (request.selection.from === request.selection.to ? "insert" : "replace")
    this.setState({
      status: "running",
      action: definition,
      instruction: this.typed,
      markdown: "",
      mode,
      hasSelection: request.selection.from !== request.selection.to,
    })

    try {
      const result: AIResult | AsyncIterable<AIStreamEvent> =
        await config.execute(request, { signal: abort.signal })
      if (runId !== this.runId) return

      let markdown: string
      let resultMode = mode
      if (isAsyncIterable<AIStreamEvent>(result)) {
        let final: string | undefined
        for await (const event of result) {
          // Leaving the loop also closes the host's stream.
          if (runId !== this.runId) return
          if (event.type === "delta") {
            this.buffer += event.text
            this.scheduleFlush(runId)
          } else if (event.type === "complete") {
            final = event.markdown ?? this.buffer
            break
          } else if (event.type === "error") {
            throw new Error(event.message || "The AI request failed.")
          }
        }
        if (runId !== this.runId) return
        markdown = final ?? this.buffer
      } else {
        markdown = result?.markdown ?? ""
        resultMode = result?.mode ?? mode
      }
      this.clearFlush()
      if (!markdown.trim()) throw new Error("The AI didn't return anything.")

      this.setState({
        ...this.state,
        status: "ready",
        markdown,
        mode: resultMode,
      })
      if (config.requireApproval === false && resultMode !== "preview") {
        this.accept()
      }
    } catch (cause) {
      if (runId !== this.runId || abort.signal.aborted) return
      this.clearFlush()
      const message =
        cause instanceof Error && cause.message
          ? cause.message
          : "The AI request failed."
      this.setState({ ...this.state, status: "error", error: message })
      this.host.onError({
        code: "ai-failed",
        message,
        actionId: definition ? `ai.${definition.id}` : undefined,
        cause,
      })
    } finally {
      if (this.abort === abort) this.abort = null
    }
  }

  /**
   * After the selection: below the block it ends in, so the answer becomes
   * its own paragraphs; at a caret, right there.
   */
  private insertAfter(range: EditorRange, markdown: string) {
    const { controller } = this.host
    const editor = this.host.getEditor()
    if (!editor || range.from === range.to) {
      return controller.replaceRange(range, markdown)
    }
    const $to = editor.state.doc.resolve(range.to)
    const at = $to.parent.isTextblock ? $to.after() : range.to
    return controller.replaceRange({ from: at, to: at }, markdown)
  }

  private reset() {
    this.runId += 1
    this.abort?.abort()
    this.abort = null
    this.clearFlush()
    this.target?.release()
    this.target = null
    this.request = null
    this.definition = null
    this.typed = undefined
    this.buffer = ""
    if (this.state !== IDLE_AI_STATE) this.setState(IDLE_AI_STATE)
  }

  private scheduleFlush(runId: number) {
    if (this.flushTimer) return
    this.flushTimer = setTimeout(() => {
      this.flushTimer = null
      if (runId === this.runId && this.state.status === "running") {
        this.setState({ ...this.state, markdown: this.buffer })
      }
    }, FLUSH_MS)
  }

  private clearFlush() {
    if (this.flushTimer) clearTimeout(this.flushTimer)
    this.flushTimer = null
  }

  private setState(state: AIState) {
    this.state = state
    for (const listener of this.listeners) listener()
  }
}
