import { cn } from "cn"
import { isText } from "../value"

const ARROWS = { up: "↑", down: "↓", flat: "→" } as const

export function RichFact({
  label,
  value,
  delta,
  trend,
  upIsGood,
}: {
  label?: unknown
  value?: unknown
  delta?: unknown
  trend?: unknown
  upIsGood?: unknown
}) {
  const moved = trend === "up" || trend === "down"
  const better = moved && (trend === "up") === (upIsGood !== false)
  const arrow =
    trend === "up" || trend === "down" || trend === "flat"
      ? ARROWS[trend]
      : undefined
  return (
    <dl
      data-slot="rich-fact"
      className="flex min-w-0 flex-col gap-1 rounded-(--radius-item) bg-muted/60 px-3 py-2.5"
    >
      <dt className="truncate text-2xs text-muted-foreground">
        {isText(label) ? label : ""}
      </dt>
      <dd className="text-[1.0625rem]/[1.3] font-[550] tracking-[-.2px] text-foreground tabular-nums">
        {isText(value) ? value : "—"}
      </dd>
      {(typeof delta === "string" && delta) || arrow ? (
        <dd
          className={cn(
            "text-2xs tabular-nums",
            !moved
              ? "text-muted-foreground"
              : better
                ? "text-success-foreground"
                : "text-danger-foreground"
          )}
        >
          {arrow && <span aria-hidden="true">{arrow} </span>}
          {typeof delta === "string" ? delta : ""}
          {moved && (
            <span className="sr-only">{better ? ", better" : ", worse"}</span>
          )}
        </dd>
      ) : null}
    </dl>
  )
}
