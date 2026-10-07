import { cn } from "cn"

/**
 * Why an ingestion failed, in the code graph worker's words: its first error
 * lines (the cause comes first), credentials masked, at most 2 KB. Also what
 * an ingestion that went on left unresolved, with its own label. Long
 * messages scroll.
 */
export function IngestionFailure({
  message,
  label = "Why the ingestion failed",
  className,
}: {
  message: string
  label?: string
  className?: string
}) {
  return (
    <pre
      tabIndex={0}
      aria-label={label}
      className={cn(
        "max-h-48 overflow-auto rounded-(--radius-item) border bg-muted px-2.5 py-2 text-left font-mono text-2xs leading-relaxed break-words whitespace-pre-wrap text-foreground",
        className
      )}
    >
      {message}
    </pre>
  )
}
