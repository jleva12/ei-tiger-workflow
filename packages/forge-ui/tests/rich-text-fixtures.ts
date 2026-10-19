import { Editor } from "@tiptap/core"

import { AIController } from "../src/components/forge/rich-text-editor/ai/ai-controller.ts"
import type { EditorAIConfig } from "../src/components/forge/rich-text-editor/ai/ai-types.ts"
import { ActionRegistry } from "../src/components/forge/rich-text-editor/core/action-registry.ts"
import { createEditorController } from "../src/components/forge/rich-text-editor/core/editor-controller.ts"
import { resolveFeatures } from "../src/components/forge/rich-text-editor/core/features.ts"
import { createDocumentExtensions } from "../src/components/forge/rich-text-editor/core/schema.ts"
import type {
  EditorError,
  EditorFeatures,
  EditorPreset,
} from "../src/components/forge/rich-text-editor/types.ts"

/**
 * A headless editor (no page) with the component's document extensions.
 * Tiptap installs plugins (history, bookmarks) when it mounts a view, so
 * without one they're installed here the same way.
 */
export function makeEditor(
  markdown = "",
  {
    preset = "full",
    features,
    mentions = false,
  }: {
    preset?: EditorPreset
    features?: EditorFeatures
    mentions?: boolean
  } = {}
) {
  const editor = new Editor({
    extensions: createDocumentExtensions({
      features: resolveFeatures(preset, features),
      mention: mentions ? {} : undefined,
    }),
    content: markdown,
    contentType: "markdown",
  })
  editor.view.updateState(
    editor.state.reconfigure({ plugins: editor.extensionManager.plugins })
  )
  return editor
}

/** An editor with its controller, registry and AI wired as the component does. */
export function makeHarness(
  markdown = "",
  options: { ai?: EditorAIConfig; preset?: EditorPreset } = {}
) {
  const editor = makeEditor(markdown, { preset: options.preset })
  const errors: EditorError[] = []
  const onError = (error: EditorError) => errors.push(error)
  const registry = new ActionRegistry(onError)
  let ai: AIController | null = null
  const controller = createEditorController({
    getEditor: () => editor,
    registry,
    getUrlPolicy: () => undefined,
    getAI: () => ai,
    onError,
  })
  if (options.ai) {
    ai = new AIController({
      controller,
      getEditor: () => editor,
      getConfig: () => options.ai,
      onError,
    })
  }
  return { editor, controller, registry, ai: ai!, errors }
}

/** The document position just after the first occurrence of `text`. */
export function positionAfter(editor: Editor, text: string) {
  return positionOf(editor, text) + text.length
}

/** The document position where `text` first starts. */
export function positionOf(editor: Editor, text: string) {
  let found = -1
  editor.state.doc.descendants((node, pos) => {
    if (found >= 0) return false
    if (node.isText && node.text?.includes(text)) {
      found = pos + node.text.indexOf(text)
      return false
    }
    return true
  })
  if (found < 0) throw new Error(`"${text}" isn't in the document`)
  return found
}

export const tick = () => new Promise((resolve) => setTimeout(resolve, 0))
