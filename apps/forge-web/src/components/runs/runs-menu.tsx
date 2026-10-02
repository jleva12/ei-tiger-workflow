import type { UseQueryResult } from "@tanstack/react-query"
import { Link } from "@tanstack/react-router"
import { cn } from "cn"

import { Icon } from "@/components/forge/icon"
import { CountBadge, StatusBadge } from "@/components/forge/status"
import { Button } from "@/components/ui/button"
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover"
import { Skeleton } from "@/components/ui/skeleton"
import type { ApiError } from "@/lib/api"
import { shortId, statusDisplay, type BackgroundTaskPage } from "@/lib/background-tasks"
import type { WorkspaceSearch } from "@/lib/organization-workspace"
import { isOpenRun, runLine } from "@/lib/runs"

/**
 * Its latest runs (`runs`), from a builder's header: how each stands, the
 * ones waiting for someone first to the eye, each opening its page in the
 * organization's workspace (`runSearch`), where approvals are decided and
 * failed runs retried; `all` is the page that lists them all.
 */
export function RunsMenu({
  organizationId,
  runs,
  all,
  runSearch,
}: {
  organizationId: string
  runs: UseQueryResult<BackgroundTaskPage, ApiError>
  all: { label: string; search: WorkspaceSearch }
  runSearch: (taskId: string) => WorkspaceSearch
}) {
  const items = runs.data?.items ?? []
  const open = items.filter(isOpenRun).length
  const deciding = items.filter((run) => run.status === "AWAITING_VALIDATION").length

  return (
    <Popover>
      <PopoverTrigger
        render={
          <Button
            variant="ghost"
            size="sm"
            aria-label={
              deciding
                ? `Runs: ${deciding} waiting for a decision`
                : open
                  ? `Runs: ${open} going`
                  : "Runs"
            }
          />
        }
      >
        <Icon icon="activity" data-icon="inline-start" />
        <span className="@max-[800px]/shell:sr-only">Runs</span>
        {open > 0 && (
          <CountBadge
            className={cn(deciding > 0 && "border-notice-border bg-notice-surface text-notice-accent")}
          >
            {open}
          </CountBadge>
        )}
      </PopoverTrigger>
      <PopoverContent align="end" className="w-[22rem] gap-0 p-0">
        <div className="flex items-center justify-between gap-3 border-b border-border px-3 py-2.5">
          <p className="text-xs font-medium text-foreground">Latest runs</p>
          <Link
            to="/organizations/$organizationId"
            params={{ organizationId }}
            search={all.search}
            className="text-xs text-muted-foreground hover:text-foreground hover:underline hover:underline-offset-3"
          >
            {all.label}
          </Link>
        </div>
        {runs.isPending ? (
          <div className="flex flex-col gap-2 p-3" aria-busy="true">
            <Skeleton className="h-9 w-full" />
            <Skeleton className="h-9 w-full" />
          </div>
        ) : runs.error ? (
          <p className="p-3 text-xs/[1.6] text-destructive">
            {runs.error.status === 503
              ? "Runs are unavailable: the async worker isn't answering."
              : `Couldn't load the runs: ${runs.error.message}`}
          </p>
        ) : items.length === 0 ? (
          <p className="p-3 text-xs/[1.6] text-muted-foreground">
            No runs yet. A run shows here seconds after it starts.
          </p>
        ) : (
          <ul className="flex max-h-80 flex-col overflow-y-auto py-1">
            {items.map((run) => {
              const shown = statusDisplay(run.status, run.outcome, run.waiting_until)
              return (
                <li key={run.id}>
                  <Link
                    to="/organizations/$organizationId"
                    params={{ organizationId }}
                    search={runSearch(run.id)}
                    className="flex items-center gap-3 px-3 py-2 hover:bg-accent focus-visible:bg-accent"
                  >
                    <span className="flex min-w-0 flex-1 flex-col gap-0.5">
                      <span className="font-mono text-2xs text-subtle">{shortId(run.id)}</span>
                      <span className="truncate text-xs text-foreground">{runLine(run)}</span>
                    </span>
                    <StatusBadge status={shown.status} className="shrink-0">
                      {shown.label}
                    </StatusBadge>
                  </Link>
                </li>
              )
            })}
          </ul>
        )}
      </PopoverContent>
    </Popover>
  )
}
