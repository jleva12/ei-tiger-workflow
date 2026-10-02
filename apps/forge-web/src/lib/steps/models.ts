import { useAssistantModels } from "@/lib/assistant-models"
import { ADK_APP, FORGE_AGENT_SERVER } from "@/lib/forge-agent"
/**
 * One of the models Forge offers (model_provider.yaml, as the assistant
 * lists them): its provider's key and its id. Both empty: the default.
 */
type ModelChoice = { provider: string; name: string }

/**
 * The models an LLM agent may run on: Forge's, as the assistant's model
 * section lists them (the admin API's model provider configuration, the
 * same model_provider.yaml the ADK workflow runner reads), its default first.
 * None while they load, or when the assistant's agent isn't Forge's own.
 */
export function useAgentStepModels() {
  return useAssistantModels(ADK_APP, FORGE_AGENT_SERVER)
}

/**
 * The model a step names, as the list's IDs go (`provider/model`); just the
 * model when the step names no provider (the runner takes it when one
 * provider has it); "" for the default.
 */
export function modelIdOf(model: ModelChoice): string {
  const provider = model.provider.trim()
  const name = model.name.trim()
  if (!name) return ""
  return provider ? `${provider}/${name}` : name
}

/** A listed model (`provider/model`) as a step names it; "" for the default. */
export function modelChoiceOf(id: string): ModelChoice {
  const slash = id.indexOf("/")
  return slash < 0
    ? { provider: "", name: id }
    : { provider: id.slice(0, slash), name: id.slice(slash + 1) }
}

/**
 * The listed model a step's model is, as the runner finds it: by its ID,
 * else by the model alone when one provider has it.
 */
export function findModel<M extends { id: string }>(models: M[], model: ModelChoice): M | undefined {
  const id = modelIdOf(model)
  if (!id) return undefined
  const exact = models.find((each) => each.id === id)
  if (exact) return exact
  const name = model.name.trim()
  const named = models.filter((each) => modelChoiceOf(each.id).name === name)
  return named.length === 1 ? named[0] : undefined
}
