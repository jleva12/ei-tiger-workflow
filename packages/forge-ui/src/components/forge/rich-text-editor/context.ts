import * as React from "react"
import { useEditorState, type Editor } from "@tiptap/react"

import type { AIController } from "./ai/ai-controller"
import {
  createActionContext,
  type ActionRegistry,
  type ActionState,
} from "./core/action-registry"
import { isAlive } from "./core/lifecycle"
import type {
  ActionSource,
  EditorAction,
  EditorController,
  EditorUiHandlers,
  ResolvedEditorFeatures,
  UrlPolicy,
} from "./types"

/** Everything an editor's UI pieces share. */
export interface RichTextEditorContextValue {
  editor: Editor
  controller: EditorController
  registry: ActionRegistry
  ai: AIController
  features: ResolvedEditorFeatures
  ui: () => EditorUiHandlers
  /** The link and image URL policy in force. */
  urlPolicy: () => UrlPolicy
}

export const RichTextEditorContext =
  React.createContext<RichTextEditorContextValue | null>(null)

/** The surrounding editor, for toolbars and menus rendered inside it. */
export function useRichTextEditorContext() {
  const context = React.useContext(RichTextEditorContext)
  if (!context) {
    throw new Error(
      "useRichTextEditorContext needs a RichTextEditor (or RichTextEditorProvider) around it."
    )
  }
  return context
}

export interface ResolvedAction {
  /** The id as asked for (an alias like `code` stays `code`). */
  id: string
  action: EditorAction
  state: ActionState
}

const HIDDEN: ActionState = {
  visible: false,
  enabled: false,
  active: false,
  running: false,
}

/**
 * The actions behind `ids` with their state at the current selection
 * (visible, enabled, pressed, running). Re-renders when the selection,
 * document or registry changes; unknown ids are left out.
 */
export function useEditorActions(
  ids: string[],
  source: ActionSource = "toolbar"
): ResolvedAction[] {
  const { editor, controller, registry } = useRichTextEditorContext()
  // A new or replaced action, or one starting or finishing, re-renders.
  React.useSyncExternalStore(registry.subscribe, registry.getVersion)
  const key = ids.join("\u0000")
  const encoded = useEditorState({
    editor,
    // The controller reads the current editor; the snapshot's can be one
    // Tiptap already replaced (StrictMode builds the first one twice).
    selector: () => {
      if (!isAlive(controller.getTiptapEditor())) return ""
      const context = createActionContext(controller, source)
      return key
        .split("\u0000")
        .map((id) => {
          const state = registry.state(id, context)
          return [state.visible, state.enabled, state.active, state.running]
            .map(Number)
            .join("")
        })
        .join(",")
    },
  })
  const states = encoded ? encoded.split(",") : []

  return ids.flatMap((id, index) => {
    const action = registry.get(id)
    if (!action) return []
    const bits = states[index]
    const state: ActionState = bits
      ? {
          visible: bits[0] === "1",
          enabled: bits[1] === "1",
          active: bits[2] === "1",
          running: bits[3] === "1",
        }
      : HIDDEN
    return [{ id, action, state }]
  })
}

/** Runs an action by id with a fresh snapshot of the selection. */
export function useRunAction(source: ActionSource = "toolbar") {
  const { controller, registry } = useRichTextEditorContext()
  return React.useCallback(
    (id: string) =>
      void registry.run(id, createActionContext(controller, source)),
    [controller, registry, source]
  )
}
