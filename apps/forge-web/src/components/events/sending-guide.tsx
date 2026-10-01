import { cn } from "cn"

import { curlExample } from "@/lib/events"
import { CopyButton } from "./json-view"

// What the endpoint answers, in the order a sender meets them.
const ANSWERS = [
  ["202", "Kept, and it matches the event type's schema."],
  ["200", "Already received with this Idempotency-Key; nothing new is kept."],
  ["422", "Kept, but it doesn't match; the answer says where."],
  ["401 / 403", "A missing or wrong token, or the endpoint is off."],
  ["404 / 409", "No event type with that key, or it's a draft or paused."],
] as const

/**
 * How another system sends the organization an event: the request, ready to copy,
 * and what each answer means. Without the endpoint's URL (it isn't on
 * yet), only how it will work.
 */
export function SendingGuide({
  url,
  eventKey = "incident.opened",
  className,
}: {
  /** The endpoint's URL, before the event type's key. */
  url: string | undefined
  eventKey?: string
  className?: string
}) {
  const command = url ? curlExample(url, eventKey, { id: "INC-1042" }) : ""
  return (
    <section
      aria-labelledby="sending-events"
      className={cn("flex min-w-0 flex-col gap-4", className)}
    >
      <div className="flex flex-col gap-1">
        <h2 id="sending-events" className="text-sm font-medium">
          Sending events
        </h2>
        <p className="max-w-[62ch] text-[0.8125rem] text-muted-foreground">
          POST each event's JSON to the endpoint's URL followed by its type's
          key, with the token as a bearer token (or an X-Forge-Token header for
          senders that can't set Authorization). An Idempotency-Key makes
          retries safe.
        </p>
      </div>
      {url ? (
        <div className="flex min-w-0 flex-col gap-2">
          <pre
            tabIndex={0}
            className="overflow-auto rounded-(--radius-card) border bg-muted px-3.5 py-3 font-mono text-2xs/[1.7] whitespace-pre text-foreground"
          >
            {command}
          </pre>
          <div>
            <CopyButton value={command} label="Copy command" />
          </div>
        </div>
      ) : (
        <p className="rounded-(--radius-card) border border-dashed px-4 py-5 text-xs text-muted-foreground">
          The request to copy shows here once the endpoint is on.
        </p>
      )}
      <dl className="grid grid-cols-[5.5rem_minmax(0,1fr)] border-t text-xs">
        {ANSWERS.map(([code, meaning]) => (
          <div key={code} className="contents">
            <dt className="border-b py-2 font-mono text-2xs text-foreground tabular-nums">
              {code}
            </dt>
            <dd className="border-b py-2 text-muted-foreground">{meaning}</dd>
          </div>
        ))}
      </dl>
    </section>
  )
}
