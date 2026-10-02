import { FollowupSuggestionsRow } from "@/components/assistant-ui/elements/follow-up-suggestions.aui"
import { useFollowUps } from "./lib/follow-ups"

/**
 * Above the composer after a reply: three questions the person might ask
 * next, written by the chat API. Clicking one sends it.
 */
export function FollowUps({
  adkUrl,
  appName,
  userId,
}: {
  adkUrl: string | undefined
  appName: string
  userId: string
}) {
  const suggestions = useFollowUps(adkUrl, appName, userId)
  if (suggestions.length === 0) return null
  return (
    <div className="animate-in duration-300 fade-in slide-in-from-bottom-1">
      <FollowupSuggestionsRow
        suggestions={suggestions.map((prompt) => ({ prompt }))}
      />
    </div>
  )
}
