import { duration, exactTokens, usd } from "./lib/format"
import type { TurnStats } from "./lib/turn-stats"

export function TurnDetails({ turn }: { turn: TurnStats }) {
  const rows: [string, string | number][] = [
    ["Model", turn.model?.split("/").at(-1) ?? "Default"],
    ["Thinking level", turn.thinkingLevel ?? "Default"],
    ["Elapsed", duration(turn.seconds)],
    ["Model calls", turn.calls],
    ["Input tokens", exactTokens(turn.inputTokens)],
    ["Cached input", exactTokens(turn.cachedTokens)],
    ["Output tokens", exactTokens(turn.outputTokens)],
    ["Reasoning tokens", exactTokens(turn.reasoningTokens)],
    ["Estimated cost", turn.costKnown ? usd(turn.cost) : "Unavailable"],
  ]
  return (
    <div>
      <dl className="space-y-2">
        {rows.map(([label, value]) => (
          <div
            key={label}
            className="flex items-start justify-between gap-4 text-xs"
          >
            <dt className="text-muted-foreground">{label}</dt>
            <dd className="min-w-0 text-end font-medium wrap-anywhere tabular-nums">
              {value}
            </dd>
          </div>
        ))}
      </dl>
      <p className="mt-4 border-t pt-3 text-2xs leading-relaxed text-muted-foreground">
        Totals for this response, across all model calls. Cost uses the model’s
        configured rates.
      </p>
    </div>
  )
}
