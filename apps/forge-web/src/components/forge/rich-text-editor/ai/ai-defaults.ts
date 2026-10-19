import type { AIActionDefinition } from "./ai-types.ts"

/** What the editor offers when `ai.actions` isn't given. */
export const DEFAULT_AI_ACTIONS: AIActionDefinition[] = [
  {
    id: "rewrite",
    label: "Rewrite",
    description: "Say the selection more clearly",
    prompt:
      "Rewrite the selected text so it reads more clearly. Keep its meaning and its Markdown formatting.",
    requiresSelection: true,
    resultMode: "replace",
  },
  {
    id: "fix",
    label: "Fix spelling and grammar",
    description: "Correct mistakes without changing the wording",
    prompt:
      "Fix spelling, grammar and punctuation in the selected text. Change nothing else.",
    requiresSelection: true,
    resultMode: "replace",
  },
  {
    id: "summarize",
    label: "Summarize",
    description: "A short summary to place where you choose",
    prompt:
      "Summarize the selected text, or the whole document when nothing is selected, in a few sentences.",
    resultMode: "preview",
  },
  {
    id: "expand",
    label: "Expand",
    description: "Add detail after the selection",
    prompt: "Write more about the selected text, adding detail and examples.",
    requiresSelection: true,
    resultMode: "insert",
  },
  {
    id: "continue",
    label: "Continue writing",
    description: "Keep going from the caret",
    prompt: "Continue writing from the caret, in the same voice and format.",
    resultMode: "insert",
  },
]

/** "Ask AI…": the instruction is whatever the person types. */
export const ASK_AI: AIActionDefinition = {
  id: "ask",
  label: "Ask AI…",
  description: "Tell the AI what to write or change",
}
