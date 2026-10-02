import { useAssistantSettings } from "@/components/forge/assistant"
import { composerChip } from "@/components/forge/assistant"
import { StatsList } from "./stats-list"
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import type { AgentModel } from "./lib/assistant-models"
import { exactTokens, usd } from "./lib/format"
import { useTurnStats, useTurnStatsSync } from "./lib/turn-stats"
import { cn } from "cn"

const percent = (fraction: number) =>
  `${(fraction * 100).toFixed(fraction < 0.01 ? 2 : 1)}%`

const RADIUS = 6
const CIRCUMFERENCE = 2 * Math.PI * RADIUS

/** A ring filled to `fraction`, amber past 70% and red past 90%. */
function Ring({ fraction }: { fraction: number }) {
  const filled = Math.min(Math.max(fraction, 0), 1)
  return (
    <svg viewBox="0 0 16 16" className="size-3.5 -rotate-90" aria-hidden>
      <circle
        cx="8"
        cy="8"
        r={RADIUS}
        fill="none"
        strokeWidth="2.5"
        className="stroke-muted-foreground/20"
      />
      <circle
        cx="8"
        cy="8"
        r={RADIUS}
        fill="none"
        strokeWidth="2.5"
        strokeLinecap="round"
        strokeDasharray={CIRCUMFERENCE}
        strokeDashoffset={CIRCUMFERENCE * (1 - filled)}
        className={cn(
          "transition-[stroke-dashoffset] duration-500",
          filled > 0.9
            ? "stroke-destructive"
            : filled > 0.7
              ? "stroke-warning-foreground"
              : "stroke-foreground/70"
        )}
      />
    </svg>
  )
}

/**
 * In the composer: how full the model's context window is (the last model
 * call's input and output against the chosen model's window) and what the
 * conversation has cost so far. It also keeps the conversation's stats read
 * (`useTurnStatsSync`) for the replies' `MessageStats`.
 */
export function ContextMeter({
  adkUrl,
  appName,
  userId,
  catalog,
  defaultModel,
}: {
  adkUrl: string | undefined
  appName: string
  userId: string
  catalog: AgentModel[]
  defaultModel: string | undefined
}) {
  useTurnStatsSync(adkUrl, appName, userId, catalog, defaultModel)
  const stats = useTurnStats((state) => state.stats)
  const chosen = useAssistantSettings().modelSettings?.model ?? defaultModel
  const model = catalog.find((each) => each.id === chosen)
  if (!stats || !model || stats.contextTokens === 0) return null
  const fraction = stats.contextTokens / model.contextWindow
  const turns = Object.values(stats.turns).filter((turn) => turn.calls > 0)
  const calls = turns.reduce((sum, turn) => sum + turn.calls, 0)

  return (
    <TooltipProvider>
      <Tooltip>
        <TooltipTrigger
          render={
            <span
              tabIndex={0}
              data-slot="context-meter"
              className={cn(
                composerChip,
                "inline-flex shrink-0 cursor-default items-center text-xs whitespace-nowrap tabular-nums outline-none"
              )}
            />
          }
          aria-label={`Context ${Math.round(fraction * 100)}% full`}
        >
          <Ring fraction={fraction} />
          {/* The cost when the model has prices; how full, otherwise. The
              exact tokens are in the tooltip. */}
          {/* Just the ring when the chat is narrow; the numbers are in the tooltip. */}
          <span className="text-foreground @max-[36rem]:hidden">
            {stats.cost > 0 ? usd(stats.cost) : percent(fraction)}
          </span>
        </TooltipTrigger>
        <TooltipContent side="top" className="flex-col items-stretch">
          <StatsList
            rows={[
              ["Context", `${percent(fraction)} full`],
              ["Used", exactTokens(stats.contextTokens)],
              ["Window", exactTokens(model.contextWindow)],
              ["Model", model.name],
              ["Turns", turns.length],
              ["Model calls", calls],
              ["Conversation cost", usd(stats.cost)],
            ]}
          />
        </TooltipContent>
      </Tooltip>
    </TooltipProvider>
  )
}
