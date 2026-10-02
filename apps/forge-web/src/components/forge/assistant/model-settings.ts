import type { IconProp } from "@/components/forge/icons"

export type AssistantModel = {
  /** Sent to the agent, e.g. "gemini-2.5-pro". */
  id: string
  name: string
  description?: string
  /**
   * Models with the same group are listed together under it, e.g. their
   * provider ("OpenAI"), in the order the groups first appear.
   */
  group?: string
  /** Shown before its name, e.g. its maker's mark. */
  icon?: IconProp
  /**
   * The ids of the thinking levels it offers, e.g. `["off"]` for a model
   * that doesn't reason. Default: every level of the model section.
   */
  thinkingLevels?: string[]
}

/** How long the model thinks before it answers. */
export type ThinkingLevel = {
  /** Sent to the agent, e.g. "high". */
  id: string
  label: string
  description?: string
}

export const defaultThinkingLevels: ThinkingLevel[] = [
  { id: "off", label: "Off", description: "Answer straight away" },
  { id: "low", label: "Low", description: "A quick plan first" },
  { id: "medium", label: "Medium", description: "Balanced for most work" },
  {
    id: "high",
    label: "High",
    description: "Longest reasoning, for hard problems",
  },
]

/**
 * Every level a model provider configuration knows (model_provider.yaml, as
 * ADK workflows' LLM agents and the Forge assistant read it), from least to most thinking.
 * Pass it as `thinkingLevels` with models that say which they offer.
 */
export const extendedThinkingLevels: ThinkingLevel[] = [
  { id: "off", label: "Off", description: "Answer straight away" },
  { id: "minimal", label: "Minimal", description: "The briefest look first" },
  { id: "low", label: "Low", description: "A quick plan first" },
  { id: "medium", label: "Medium", description: "Balanced for most work" },
  {
    id: "high",
    label: "High",
    description: "Long reasoning, for hard problems",
  },
  {
    id: "xhigh",
    label: "Extra high",
    description: "Longest reasoning, for the hardest problems",
  },
]

/**
 * The level a model runs at when `id` is chosen: that one if it's among
 * `levels`, else the nearest above it in `order`, else the nearest below,
 * else the first. The same rule the server applies.
 */
export function nearestThinkingLevel(
  levels: ThinkingLevel[],
  order: ThinkingLevel[],
  id: string
): string | undefined {
  if (levels.some((level) => level.id === id)) return id
  const offered = new Set(levels.map((level) => level.id))
  const index = order.findIndex((level) => level.id === id)
  if (index >= 0) {
    const above = order.slice(index + 1).find((level) => offered.has(level.id))
    if (above) return above.id
    const below = order
      .slice(0, index)
      .reverse()
      .find((level) => offered.has(level.id))
    if (below) return below.id
  }
  return levels[0]?.id
}

/** What the model section has selected, sent with every run. */
export type ModelSelection = {
  model: string | undefined
  thinkingLevel: string
}

/**
 * The model section's choices and selection, from `useAdkAssistant`. Pass it
 * to `AssistantScreen` with `showModels`.
 */
export type ModelSettings = ModelSelection & {
  models: AssistantModel[]
  /** The selected model's levels. */
  thinkingLevels: ThinkingLevel[]
  setModel: (id: string) => void
  setThinkingLevel: (id: string) => void
  /**
   * Called by the model section while it's mounted: the selection is only
   * sent while someone can see it. Returns the cleanup.
   */
  show: () => () => void
}

/** The default ADK state delta: `model` and `thinking_level` session keys. */
export const defaultModelStateDelta = ({
  model,
  thinkingLevel,
}: ModelSelection): Record<string, unknown> => ({
  ...(model && { model }),
  thinking_level: thinkingLevel,
})
