import { isGone, isLimited } from "@/features/agents/lib/chat-page"
import { toApiError } from "@/lib/api/index"

/** Why a page didn't answer, in the words of someone adding it. */
export function failureOf(caught: unknown, server: string) {
  if (isGone(caught))
    return "There's no chat page at this link on that server: it was turned off or given a new link, or it's on another server."
  if (isLimited(caught))
    return "This agent is only for people its organization lets use it, so its chat page doesn't answer."
  const error = toApiError(caught)
  if (error.kind === "network" || error.kind === "timeout")
    return `Couldn't reach ${server}. Check the address, and that the server lets this app call it (FORGE_ADMIN_CORS_ORIGINS).`
  return error.message
}
