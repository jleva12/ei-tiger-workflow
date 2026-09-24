/** A model the agent can run on, picked from the composer's model section. */
export type AssistantModel = {
  /** Sent to the agent, e.g. "gemini-2.5-pro". */
  id: string
  name: string
  description?: string
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
