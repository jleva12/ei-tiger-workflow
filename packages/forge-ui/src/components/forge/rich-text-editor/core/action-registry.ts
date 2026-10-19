import type {
  ActionSource,
  EditorAction,
  EditorActionContext,
  EditorController,
  EditorError,
} from "../types.ts"

/** Other names the toolbar and menus accept for built-in actions. */
const ALIASES: Record<string, string> = {
  code: "inlineCode",
  strikethrough: "strike",
  quote: "blockquote",
  divider: "horizontalRule",
  hr: "horizontalRule",
  checklist: "taskList",
  h1: "heading1",
  h2: "heading2",
  h3: "heading3",
}

export function resolveActionId(id: string) {
  return ALIASES[id] ?? id
}

/** What a menu or toolbar shows for an action at the current selection. */
export interface ActionState {
  visible: boolean
  enabled: boolean
  active: boolean
  running: boolean
}

/**
 * The snapshot an action runs with. The document's Markdown is serialized on
 * first read, so building a context per menu entry stays cheap.
 */
export function createActionContext(
  controller: EditorController,
  source: ActionSource
): EditorActionContext {
  const selection = Object.freeze(controller.getSelection())
  let markdown: string | undefined
  return Object.freeze({
    editor: controller,
    selection,
    selectedText: selection.text,
    get documentMarkdown() {
      return (markdown ??= controller.getMarkdown())
    },
    readOnly: controller.isReadOnly(),
    source,
  })
}

/** The fields menus and shortcuts depend on, to tell real changes apart. */
const signature = (actions: EditorAction[]) =>
  actions
    .map((action) =>
      [
        action.id,
        action.label,
        action.group,
        action.shortcut,
        action.slashCommand,
        typeof action.icon === "string" ? action.icon : "",
      ].join("\u0000")
    )
    .join("\u0001")

/**
 * Every action the editor knows, by id: the built-ins, the `actions` prop
 * and ones registered at runtime, later sources replacing earlier ones with
 * the same id. Running is guarded, so an action can't start twice at once,
 * and failures go to `onError` instead of throwing into a click handler.
 */
export class ActionRegistry {
  private builtIns = new Map<string, EditorAction>()
  private custom = new Map<string, EditorAction>()
  private runtime = new Map<string, EditorAction>()
  private running = new Set<string>()
  private listeners = new Set<() => void>()
  private version = 0
  private customSignature = ""
  private onError: (error: EditorError) => void

  constructor(onError: (error: EditorError) => void) {
    this.onError = onError
  }

  setBuiltIns(actions: EditorAction[]) {
    this.builtIns = new Map(actions.map((action) => [action.id, action]))
    this.notify()
  }

  /** Replaces the `actions` prop's set; only a visible change re-renders menus. */
  setCustom(actions: EditorAction[]) {
    this.custom = new Map(actions.map((action) => [action.id, action]))
    const next = signature(actions)
    if (next !== this.customSignature) {
      this.customSignature = next
      this.notify()
    }
  }

  register(action: EditorAction) {
    this.runtime.set(action.id, action)
    this.notify()
    return () => {
      if (this.runtime.get(action.id) !== action) return
      this.runtime.delete(action.id)
      this.notify()
    }
  }

  get(id: string): EditorAction | undefined {
    const key =
      this.runtime.has(id) || this.custom.has(id) ? id : resolveActionId(id)
    return (
      this.runtime.get(key) ?? this.custom.get(key) ?? this.builtIns.get(key)
    )
  }

  /** Built-ins first, then custom and runtime ones, each id once. */
  list(): EditorAction[] {
    const merged = new Map<string, EditorAction>()
    for (const source of [this.builtIns, this.custom, this.runtime]) {
      for (const [id, action] of source) merged.set(id, action)
    }
    return [...merged.values()]
  }

  /** Whether `id` is the editor's own action, not replaced by the app. */
  isBuiltIn(id: string) {
    return (
      this.builtIns.has(id) && !this.custom.has(id) && !this.runtime.has(id)
    )
  }

  isRunning(id: string) {
    return this.running.has(resolveActionId(id))
  }

  state(id: string, context: EditorActionContext): ActionState {
    const action = this.get(id)
    if (!action) {
      return { visible: false, enabled: false, active: false, running: false }
    }
    const visible = this.safely(action, () => action.isVisible?.(context), true)
    const allowed = !context.readOnly || Boolean(action.allowReadOnly)
    const enabled =
      visible &&
      allowed &&
      this.safely(action, () => action.isEnabled?.(context), true)
    return {
      visible,
      enabled,
      active: this.safely(action, () => action.isActive?.(context), false),
      running: this.running.has(action.id),
    }
  }

  /**
   * Runs an action with `context`. Resolves to whether it ran: unknown,
   * disabled, read-only and already-running actions don't, and a failing
   * one reports through `onError`.
   */
  async run(id: string, context: EditorActionContext): Promise<boolean> {
    const action = this.get(id)
    if (!action) {
      this.onError({
        code: "unknown-action",
        message: `There's no editor action "${id}".`,
        actionId: id,
      })
      return false
    }
    if (this.running.has(action.id)) return false
    if (context.readOnly && !action.allowReadOnly) {
      this.onError({
        code: "read-only",
        message: `"${action.label}" can't run while the editor is read-only.`,
        actionId: action.id,
      })
      return false
    }
    const { visible, enabled } = this.state(action.id, context)
    if (!visible || !enabled) return false

    this.running.add(action.id)
    this.notify()
    try {
      await action.execute(context)
      return true
    } catch (cause) {
      this.onError({
        code: "action-failed",
        message:
          cause instanceof Error ? cause.message : `"${action.label}" failed.`,
        actionId: action.id,
        cause,
      })
      return false
    } finally {
      this.running.delete(action.id)
      this.notify()
    }
  }

  subscribe = (listener: () => void) => {
    this.listeners.add(listener)
    return () => {
      this.listeners.delete(listener)
    }
  }

  getVersion = () => this.version

  private notify() {
    this.version += 1
    for (const listener of this.listeners) listener()
  }

  /** A predicate that throws counts as `fallback`, and is reported. */
  private safely(
    action: EditorAction,
    read: () => boolean | undefined,
    fallback: boolean
  ) {
    try {
      return read() ?? fallback
    } catch (cause) {
      this.onError({
        code: "action-failed",
        message: `"${action.label}" couldn't check whether it applies.`,
        actionId: action.id,
        cause,
      })
      return fallback
    }
  }
}
