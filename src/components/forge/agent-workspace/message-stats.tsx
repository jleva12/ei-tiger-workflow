import { useAuiState } from "@assistant-ui/react"
import { Icon } from "@/components/forge/icon"
import { TurnDetails } from "./turn-details"
import {
  Popover,
  PopoverContent,
  PopoverTitle,
  PopoverTrigger,
} from "@/components/ui/popover"
import { duration, tokens, usd } from "./lib/format"
import { eventIdOf, useTurnStats } from "./lib/turn-stats"

/** Persisted turn totals, available by click, touch, or keyboard. */
export function MessageStats() {
  const messageId = useAuiState((s) => s.message.id)
  const sessionId = useAuiState((s) => s.threadListItem.remoteId)
  const running = useAuiState((s) => s.message.status?.type === "running")
  const replyIds = useAuiState((s) =>
    s.thread.messages
      .filter((m) => m.role === "assistant")
      .map((m) => m.id)
      .join(" ")
  )
  const stats = useTurnStats((s) => s.stats)
  if (running || !stats || stats.sessionId !== sessionId) return null
  const turnId = stats.eventTurn[eventIdOf(messageId)]
  const turn = turnId ? stats.turns[turnId] : undefined
  if (!turn || turn.calls === 0) return null
  if (
    replyIds
      .split(" ")
      .filter((id) => stats.eventTurn[eventIdOf(id)] === turnId)
      .at(-1) !== messageId
  )
    return null
  return (
    <Popover>
      <PopoverTrigger
        aria-label="View response statistics"
        data-slot="message-stats"
        className="ms-1 inline-flex min-h-7 items-center gap-1.5 rounded-md px-2 text-2xs text-muted-foreground tabular-nums transition-colors hover:bg-muted hover:text-foreground data-popup-open:bg-muted"
      >
        <Icon icon="activity" size={12} />
        <span>{duration(turn.seconds)}</span>
        <span aria-hidden>·</span>
        <span>{tokens(turn.outputTokens)} tokens out</span>
        {turn.costKnown && turn.cost > 0 && (
          <>
            <span aria-hidden>·</span>
            <span>{usd(turn.cost)}</span>
          </>
        )}
        <Icon icon="down" size={11} />
      </PopoverTrigger>
      <PopoverContent
        side="top"
        align="start"
        className="w-72 max-w-[calc(100vw-2rem)] p-4"
      >
        <PopoverTitle className="text-sm">Response statistics</PopoverTitle>
        <TurnDetails turn={turn} />
      </PopoverContent>
    </Popover>
  )
}
