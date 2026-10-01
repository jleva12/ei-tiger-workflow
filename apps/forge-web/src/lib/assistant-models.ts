import * as React from "react"
import {
  ChatGptIcon,
  ClaudeIcon,
  GoogleGeminiIcon,
} from "@hugeicons/core-free-icons"
import { useQuery } from "@tanstack/react-query"

import type { AssistantModel } from "@/components/forge/assistant"
import type { IconProp } from "@/components/forge/icons"
import { api } from "@/lib/api-instance"

/**
 * A model the Forge assistant's conversations may run on, as the admin API's
 * model provider configuration gives it (the shared model_provider.yaml
 * workflows' agent steps read too). Nothing about how it's reached.
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
}

export type AgentModels = {
  /** What a conversation runs on until it chooses; null when it can't. */
  defaultModel: string | null
  models: AgentModel[]
}

export const assistantModelKeys = {
  all: ["assistant-models"] as const,
}

const compact = new Intl.NumberFormat("en", { notation: "compact" })

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
 * The models the agent `appName` of the admin API lets a conversation choose,
 * its default first. Only while `enabled`: another ADK server doesn't say.
 */
export function useAssistantModels(appName: string, enabled: boolean) {
  const query = useQuery({
    queryKey: [...assistantModelKeys.all, appName],
    queryFn: ({ signal }) =>
      api.get<AgentModels>(
        `/agents/apps/${encodeURIComponent(appName)}/models`,
        { signal }
      ),
    enabled,
    // It changes with the server's configuration, not by the minute.
    staleTime: 5 * 60_000,
    // The assistant works without it, on the agent's default.
    meta: { silent: true },
  })
  const models = React.useMemo(
    () => query.data?.models.map(toAssistantModel) ?? [],
    [query.data]
  )
  return {
    models,
    defaultModel: query.data?.defaultModel ?? undefined,
    isLoading: query.isLoading,
    isError: query.isError,
  }
}
