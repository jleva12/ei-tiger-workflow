import * as React from "react"
import { cn } from "cn"

import { Icon } from "@/components/forge/icon"
import type { BuilderIssue } from "@/features/builder/lib/types"
import { FieldIssuesContext } from "./field-issues-context"

/* Where a step's issues reach its fields, and how a field shows its own. */

/** The step's issues, for the fields inside. */
export function FieldIssuesProvider({
  issues,
  children,
}: {
  issues: BuilderIssue[]
  children: React.ReactNode
}) {
  const value = React.useMemo(() => ({ issues, prefix: "" }), [issues])
  return (
    <FieldIssuesContext.Provider value={value}>
      {children}
    </FieldIssuesContext.Provider>
  )
}

/**
 * The fields inside hold settings under `prefix` (a sub-agent's:
 * `agents.<id>.`), inside the prefix around it; `whole`: `prefix` is
 * their whole prefix (a card opened from inside a sub-agent's settings).
 */
export function FieldIssuesPrefix({
  prefix,
  whole = false,
  children,
}: {
  prefix: string
  whole?: boolean
  children: React.ReactNode
}) {
  const outer = React.useContext(FieldIssuesContext)
  const value = React.useMemo(
    () => ({
      issues: outer.issues,
      prefix: whole ? prefix : outer.prefix + prefix,
    }),
    [outer, prefix, whole]
  )
  return (
    <FieldIssuesContext.Provider value={value}>
      {children}
    </FieldIssuesContext.Provider>
  )
}

/** Issues under a field: an error in red, a warning in amber, as an expression's problems show. */
export function IssueMessages({
  issues,
  className,
}: {
  issues: Pick<BuilderIssue, "id" | "level" | "message">[]
  className?: string
}) {
  if (!issues.length) return null
  return (
    <div aria-live="polite" className={cn("flex flex-col gap-1", className)}>
      {issues.map((issue) => (
        <p
          key={issue.id}
          className={cn(
            "flex items-start gap-1.5 text-xs/[1.5]",
            issue.level === "error"
              ? "text-destructive"
              : "text-tone-amber-foreground"
          )}
        >
          <Icon
            icon={issue.level === "error" ? "failed" : "warning"}
            size={13}
            className="mt-0.5 shrink-0"
          />
          {issue.message}
        </p>
      ))}
    </div>
  )
}
