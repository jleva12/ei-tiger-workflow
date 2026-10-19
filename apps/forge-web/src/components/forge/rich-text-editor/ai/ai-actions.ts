import {
  AiChat02Icon,
  ArrowExpandIcon,
  ArrowRight02Icon,
  Note01Icon,
  PencilEdit02Icon,
  SparklesIcon,
  Tick02Icon,
} from "@hugeicons/core-free-icons"

import type { EditorAction, EditorUiHandlers } from "../types.ts"
import { ASK_AI, type AIController } from "./ai-controller.ts"

const ICONS: Record<string, EditorAction["icon"]> = {
  ask: AiChat02Icon,
  rewrite: PencilEdit02Icon,
  fix: Tick02Icon,
  summarize: Note01Icon,
  expand: ArrowExpandIcon,
  continue: ArrowRight02Icon,
}

/**
 * The AI commands as editor actions (`ai.<id>`), so menus, toolbars, the `/`
 * menu and `executeAction` reach them like any other action. "Ask AI…" and
 * any action that asks for an instruction open the prompt box instead of
 * running straight away.
 */
export function createAIActions(
  ai: AIController,
  ui: () => EditorUiHandlers
): EditorAction[] {
  return ai.definitions().map((definition): EditorAction => ({
    id: `ai.${definition.id}`,
    label: definition.label,
    icon: ICONS[definition.id] ?? SparklesIcon,
    group: "AI",
    description: definition.description,
    keywords: ["ai", "assistant", definition.id],
    slashCommand: !definition.requiresSelection,
    isEnabled: (context) =>
      !definition.requiresSelection || !context.selection.empty,
    execute: async (context) => {
      const { from, to } = context.selection
      if (definition.id === ASK_AI.id || definition.askInstruction) {
        ai.openPrompt({ from, to }, definition.id)
        ui().askAI()
        return
      }
      await ai.start(definition.id, { range: { from, to } })
    },
  }))
}
