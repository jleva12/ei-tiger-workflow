import { cn } from "cn"

import { Icon } from "@/components/forge/icon"
import type { BuilderIssue } from "@/features/builder/lib/types"
import { useBuilder } from "./store"

/**
 * Issues as a list; each opens its step's settings when there's a step to
 * open, at the setting it's about.
 */
export function IssueList({
  issues,
  onOpen,
  within,
}: {
  issues: BuilderIssue[]
  onOpen?: (step: string, field?: string) => void
  /** In the step's own settings: no step names, and only a setting's issues go to it. */
  within?: boolean
}) {
  const names = useBuilder((s) => s.nodes)
  return (
    <ul className="-mx-1.5 flex flex-col">
      {issues.map((issue) => {
        const step = issue.step
          ? names.find((n) => n.id === issue.step)
          : undefined
        const body = (
          <>
            <Icon
              icon={issue.level === "error" ? "failed" : "warning"}
              size={14}
              className={cn(
                "mt-px shrink-0",
                issue.level === "error"
                  ? "text-destructive"
                  : "text-tone-amber-foreground"
              )}
            />
            <span className="flex min-w-0 flex-col">
              {onOpen && step && !within && (
                <span className="truncate font-medium text-foreground">
                  {step.data.name}
                </span>
              )}
              <span className="text-muted-foreground">{issue.message}</span>
            </span>
          </>
        )
        return (
          <li key={issue.id}>
            {onOpen && issue.step && (!within || issue.field) ? (
              <button
                type="button"
                onClick={() => onOpen(issue.step!, issue.field)}
                className="flex w-full items-start gap-2 rounded-(--radius-item) px-1.5 py-1.5 text-left text-xs/[1.5] transition-colors duration-150 hover:bg-muted"
              >
                {body}
              </button>
            ) : (
              <div className="flex items-start gap-2 px-1.5 py-1 text-xs/[1.5]">
                {body}
              </div>
            )}
          </li>
        )
      })}
    </ul>
  )
}
