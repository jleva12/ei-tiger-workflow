import type { EditorRange } from "../types.ts"

/**
 * Where a finished AI result goes: `replace` swaps the text the action ran
 * on, `insert` adds it after that text (or at the caret), `append` adds it at
 * the end of the document, and `preview` only shows it, so the person picks.
 */
export type AIResultMode = "insert" | "replace" | "append" | "preview"

/** One AI command the editor offers, as `ai.<id>` in menus and toolbars. */
export interface AIActionDefinition {
  id: string
  label: string
  /** Sent to `execute` as `instruction`; the host turns it into a prompt. */
  prompt?: string
  /** Only offered while text is selected. */
  requiresSelection?: boolean
  resultMode?: AIResultMode
  /** Shown in the `/` menu and as a tooltip. */
  description?: string
  /**
   * Opens the prompt box first: what the person types is sent as
   * `instruction` (after `prompt`, when there's one), like "Ask AI…".
   */
  askInstruction?: boolean
  /** The prompt box's placeholder, for an action that asks. */
  placeholder?: string
}

/** What the editor sends the host's `execute` callback. Plain JSON. */
export interface AIRequest {
  actionId: string
  instruction?: string
  selectedText: string
  /** Markdown of the selection, so formatting survives the round trip. */
  selectedMarkdown: string
  selection: EditorRange
  documentMarkdown: string
  context?: Record<string, unknown>
}

export interface AIResult {
  markdown: string
  mode?: AIResultMode
}

export type AIStreamEvent =
  | { type: "delta"; text: string }
  | { type: "complete"; markdown?: string }
  | { type: "error"; message: string }

export interface AIExecuteOptions {
  /** Aborted when the person cancels or the editor unmounts. */
  signal: AbortSignal
}

export interface EditorAIConfig {
  enabled: boolean
  /** The AI commands; without them the editor offers rewrite, fix, summarize, expand and continue. */
  actions?: AIActionDefinition[]
  /** Runs a request: return the whole result, or stream events. */
  execute: (
    request: AIRequest,
    options: AIExecuteOptions
  ) =>
    | Promise<AIResult | AsyncIterable<AIStreamEvent>>
    | AsyncIterable<AIStreamEvent>
  /** Show results for Accept / Reject before they change the document. Default true. */
  requireApproval?: boolean
  /** Offer "Ask AI…", where the person types their own instruction. Default true. */
  allowCustomPrompts?: boolean
  /** Extra data for the host, sent as `request.context`. */
  context?:
    | Record<string, unknown>
    | ((actionId: string) => Record<string, unknown> | undefined)
}

/**
 * Where the AI flow is: `prompt` while the person types an instruction,
 * `running` while the host works (text streams into `markdown`), `ready`
 * with a result waiting for approval, `conflict` when the text it was meant
 * for changed meanwhile, `error` when it failed.
 */
export type AIStatus =
  "idle" | "prompt" | "running" | "ready" | "conflict" | "error"

export interface AIState {
  status: AIStatus
  /** The action that ran, or null when idle. */
  action: AIActionDefinition | null
  /** The instruction typed into "Ask AI…", if any. */
  instruction?: string
  /** The result so far (streaming) or in full. */
  markdown: string
  mode: AIResultMode
  /** Whether the run had a selection to replace. */
  hasSelection: boolean
  error?: string
}

export const IDLE_AI_STATE: AIState = {
  status: "idle",
  action: null,
  markdown: "",
  mode: "insert",
  hasSelection: false,
}
