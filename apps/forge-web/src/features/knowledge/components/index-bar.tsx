import { cn } from "cn"

import type { DocumentTotals } from "../lib/api"
import {
  INDEX_STATE_KEYS,
  INDEX_STATES,
  type IndexState,
} from "../lib/knowledge"

/**
 * The knowledge base's documents as one bar, a segment per index state in that
 * state's own ink (ready, indexing, queued, failed, unconfirmed), split by a
 * hairline in the page colour. Empty, it's a quiet track.
 */
export function IndexBar({
  totals,
  className,
}: {
  totals: Record<IndexState, DocumentTotals>
  className?: string
}) {
  const total = INDEX_STATE_KEYS.reduce(
    (sum, key) => sum + totals[key].documents,
    0
  )
  const label = `${totals.ready.documents} of ${total} documents ready`
  return (
    <div
      role="img"
      aria-label={label}
      className={cn(
        "flex h-1.5 w-full overflow-hidden rounded-full bg-muted",
        className
      )}
    >
      {total > 0 &&
        INDEX_STATE_KEYS.map((key) =>
          totals[key].documents > 0 ? (
            <span
              key={key}
              className={cn(
                "h-full border-r border-background transition-[width] duration-300 ease-out last:border-r-0 motion-reduce:transition-none",
                INDEX_STATES[key].bar
              )}
              style={{ width: `${(totals[key].documents / total) * 100}%` }}
            />
          ) : null
        )}
    </div>
  )
}
