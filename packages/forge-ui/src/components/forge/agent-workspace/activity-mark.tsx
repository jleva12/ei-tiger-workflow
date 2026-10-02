import { Icon } from "@/components/forge/icon"
import { Spinner } from "@/components/ui/spinner"
import { type ActivityState } from "./lib/activity"
import { cn } from "cn"

export function ActivityMark({ state }: { state: ActivityState }) {
  return (
    <span
      aria-hidden="true"
      className={cn(
        "flex size-5 shrink-0 items-center justify-center rounded-full",
        state === "complete" &&
          "bg-status-success text-status-success-foreground",
        state === "waiting" &&
          "bg-status-running text-status-running-foreground",
        state === "error" && "bg-status-failed text-status-failed-foreground",
        state === "stopped" && "bg-muted text-muted-foreground"
      )}
    >
      {state === "running" ? (
        <Spinner className="size-3.5" />
      ) : (
        <Icon
          icon={
            state === "complete"
              ? "check"
              : state === "stopped"
                ? "stop"
                : "warning"
          }
          size={12}
        />
      )}
    </span>
  )
}
