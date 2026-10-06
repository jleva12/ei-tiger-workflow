import { Icon } from "@/components/forge/icon"
import { Button } from "@/components/ui/button"
import { toast } from "@/components/ui/toast"
import { cn } from "cn"

/**
 * A value to copy, under its label: an address, an ID, a key. `multiline`
 * keeps its lines (a command) rather than cutting it to one.
 */
export function Copyable({
  label,
  value,
  multiline = false,
}: {
  label: string
  value: string
  multiline?: boolean
}) {
  return (
    <div className="flex min-w-0 flex-col gap-0.5">
      <span className="text-2xs text-muted-foreground">{label}</span>
      <div
        className={cn("flex gap-1", multiline ? "items-start" : "items-center")}
      >
        <code
          className={cn(
            "min-w-0 flex-1 rounded-(--radius-control) border bg-muted/40 px-1.5 py-1 font-mono text-2xs text-foreground",
            multiline ? "overflow-x-auto whitespace-pre" : "truncate"
          )}
        >
          {value}
        </code>
        <Button
          variant="ghost"
          size="icon-xs"
          aria-label={`Copy ${label.toLowerCase()}`}
          onClick={() => {
            void navigator.clipboard?.writeText(value)
            toast.add({
              title: `Copied ${label.toLowerCase()}`,
              type: "success",
            })
          }}
        >
          <Icon icon="copy" />
        </Button>
      </div>
    </div>
  )
}
