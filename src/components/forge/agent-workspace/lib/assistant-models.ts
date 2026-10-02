import * as React from "react"
import {
  ChatGptIcon,
  ClaudeIcon,
  GoogleGeminiIcon,
} from "@hugeicons/core-free-icons"

import type { AssistantModel } from "@/components/forge/assistant"
import type { IconProp } from "@/components/forge/icons"

/**
 * A model the assistant's conversations may run on, as the chat API's model
 * provider configuration gives it. Nothing about how it's reached.
 */
export type AgentModel = {
  /** provider/model: what a run sends as its `model` state. */
  id: string
  provider: string
  providerName: string
  /** The provider's own id for it. */
  model: string
  name: string
  api: string
  reasoning: boolean
  input: string[]
  contextWindow: number
  maxTokens: number
  /** From least to most thinking: off, minimal, low, medium, high, xhigh. */
  thinkingLevels: string[]
  /** USD per million tokens. */
  cost: { input: number; output: number; cacheRead: number; cacheWrite: number }
}

export type AgentModels = {
  /** What a conversation runs on until it chooses; null when it can't. */
  defaultModel: string | null
  models: AgentModel[]
}

const compact = new Intl.NumberFormat("en", { notation: "compact" })
const noCatalog: AgentModel[] = []

/**
 * The mark of the company that makes a model: by its id first, since a
 * gateway may serve anyone's models over one API, then by the API it speaks.
 */
export function modelMakerIcon(model: AgentModel): IconProp {
  const id = model.model.toLowerCase()
  if (id.startsWith("claude")) return ClaudeIcon
  if (id.startsWith("gemini")) return GoogleGeminiIcon
  if (/^(gpt|o\d|chatgpt|codex)/.test(id)) return ChatGptIcon
  if (model.api === "anthropic-messages") return ClaudeIcon
  if (model.api === "google-generative-ai") return GoogleGeminiIcon
  if (model.api.startsWith("openai-")) return ChatGptIcon
  return "sparkles"
}

/**
 * A model as the composer's model section lists it: under its provider,
 * with its maker's mark.
 */
export function toAssistantModel(model: AgentModel): AssistantModel {
  return {
    id: model.id,
    name: model.name,
    description: `${compact.format(model.contextWindow)} context`,
    group: model.providerName,
    icon: modelMakerIcon(model),
    thinkingLevels: model.thinkingLevels,
  }
}

/**
 * The models the agent at `adkUrl` lets a conversation choose, its default
 * first; none until they arrive, or when the server doesn't say (the
 * assistant then runs on the agent's default). Only while `enabled`.
 */
export function useAssistantModels(
  adkUrl: string | undefined,
  appName: string,
  enabled: boolean
) {
  const [data, setData] = React.useState<AgentModels | null>(null)
  React.useEffect(() => {
    if (!enabled || !adkUrl) return
    const controller = new AbortController()
    fetch(`${adkUrl}/apps/${encodeURIComponent(appName)}/models`, {
      signal: controller.signal,
    })
      .then((response) => (response.ok ? response.json() : null))
      .then((body: AgentModels | null) => {
        if (body) setData(body)
      })
      // The assistant works without them, on the agent's default.
      .catch(() => {})
    return () => controller.abort()
  }, [adkUrl, appName, enabled])
  const models = React.useMemo(
    () => data?.models.map(toAssistantModel) ?? [],
    [data]
  )
  return {
    models,
    defaultModel: data?.defaultModel ?? undefined,
    /** As the server lists them, with context windows and prices. */
    catalog: data?.models ?? noCatalog,
  }
}
