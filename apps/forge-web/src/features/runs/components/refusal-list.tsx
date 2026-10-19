import * as React from "react"
import { cn } from "cn"

import { PageEmpty, PanelEmpty } from "@/components/forge/empty-state"
import { ActiveFilters, ErrorCallout } from "@/components/forge/feedback"
import { Icon } from "@/components/forge/icon"
import { Chip } from "@/components/forge/status"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import {
  ADK_RUN_REFUSALS_PAGE,
  provenanceLine,
  provenanceWords,
  type AdkRunRefusal,
  type useAdkRunRefusals,
} from "@/features/runs/lib/runs"
import { formatDateTime, formatShortDateTime } from "@/lib/format"

/*
 * The starts of the organization's workflows it refused, for an audit:
 * someone who may not run them (403: without agents:run, or using an agent
 * they may not), or what it'd run didn't fit or build (422). No run was
 * made; each says when, which workflow, how it came and for whom, why, and
 * from where.
 */

/** What each answer means. */
const ANSWERS: Record<number, { label: string; tone: "danger" | "warning" }> = {
  403: { label: "Not allowed", tone: "danger" },
  422: { label: "Didn't fit", tone: "warning" },
}

function Refusal({
  refusal,
  workflowName,
}: {
  refusal: AdkRunRefusal
  workflowName: string | undefined
}) {
  const words = provenanceWords({
    requested_by: refusal.requested_by ?? { id: null, name: "Unknown" },
    resubmit_of: null,
    provenance: refusal.provenance,
  })
  const answer = ANSWERS[refusal.status_code] ?? {
    label: String(refusal.status_code),
    tone: "warning" as const,
  }
  const person = refusal.provenance?.on_behalf_of
  return (
    <TableRow>
      <TableCell className="whitespace-nowrap">
        <Tooltip>
          <TooltipTrigger render={<span className="text-foreground" />}>
            {formatShortDateTime(refusal.at)}
          </TooltipTrigger>
          <TooltipContent>{formatDateTime(refusal.at)}</TooltipContent>
        </Tooltip>
      </TableCell>
      <TableCell className="max-w-52">
        <span className="block truncate">
          {workflowName ?? refusal.agent_id}
        </span>
        <span className="mt-0.5 block truncate font-mono text-3xs text-muted-foreground">
          {refusal.agent_id}
          {refusal.version ? `@${refusal.version}` : ""}
        </span>
      </TableCell>
      <TableCell className="max-w-72">
        <span className="block truncate" title={provenanceLine(words)}>
          {words.how} · {words.by}
        </span>
        {(words.channel || words.credential) && (
          <span className="mt-0.5 block truncate text-3xs text-muted-foreground">
            {[words.channel && `Through ${words.channel}`, words.credential]
              .filter(Boolean)
              .join(" · ")}
          </span>
        )}
      </TableCell>
      <TableCell className="max-w-56">
        {person ? (
          <span className="flex min-w-0 items-center gap-1.5">
            <span className="truncate" title={person.id}>
              {person.name || person.id}
            </span>
            {person.verified === false && (
              <Chip tone="warning" className="shrink-0">
                Unverified
              </Chip>
            )}
          </span>
        ) : (
          <span className="text-subtle">—</span>
        )}
      </TableCell>
      <TableCell className="max-w-96 min-w-56 whitespace-normal">
        <Chip tone={answer.tone}>{answer.label}</Chip>
        <span
          className="mt-1 line-clamp-2 text-xs text-muted-foreground"
          title={refusal.reason}
        >
          {refusal.reason}
        </span>
      </TableCell>
      <TableCell className="whitespace-nowrap">
        {refusal.origin?.client_ip ? (
          <span
            className="font-mono text-3xs text-muted-foreground"
            title={
              [refusal.origin.user_agent, refusal.origin.request_id]
                .filter(Boolean)
                .join(" · ") || undefined
            }
          >
            {refusal.origin.client_ip}
          </span>
        ) : (
          <span className="text-subtle">—</span>
        )}
      </TableCell>
    </TableRow>
  )
}

/**
 * The organization's refused starts, newest first, a page at a time.
 *
 * :param list: The page (`useAdkRunRefusals`), shared with the tab's count.
 * :param workflowNames: The organization's workflows' names, by ID.
 */
export function RefusalList({
  organizationName,
  list,
  page,
  onPage,
  workflowNames,
  filters,
  filtered,
  onClearFilters,
}: {
  organizationName: string
  list: ReturnType<typeof useAdkRunRefusals>
  page: number
  onPage: (page: number) => void
  workflowNames: Map<string, string>
  /** The filters over the list (RunFiltersBar), under its title. */
  filters: React.ReactNode
  filtered: boolean
  onClearFilters: () => void
}) {
  const data = list.data
  let body: React.ReactNode
  if (list.error && !data) {
    body =
      list.error.status === 503 ? (
        <PageEmpty
          illustration="offline"
          title="Refused starts are unavailable"
          description={list.error.message}
        >
          <Button
            variant="outline"
            size="sm"
            onClick={() => void list.refetch()}
          >
            Retry
          </Button>
        </PageEmpty>
      ) : (
        <ErrorCallout
          title="Couldn't load the refused starts"
          action={
            <Button
              variant="outline"
              size="sm"
              onClick={() => void list.refetch()}
            >
              Retry
            </Button>
          }
        >
          {list.error.message}
        </ErrorCallout>
      )
  } else if (!data) {
    body = (
      <div className="flex flex-col gap-2" aria-busy="true">
        {Array.from({ length: 5 }, (_, index) => (
          <Skeleton key={index} className="h-11 w-full" />
        ))}
      </div>
    )
  } else if (data.items.length === 0) {
    body = filtered ? (
      <PanelEmpty illustration="search">
        No refused start matches. Try another way, person, agent or day.
      </PanelEmpty>
    ) : (
      <PanelEmpty illustration="complete">
        {organizationName} hasn't refused a run: every start was allowed and fit
        its workflow.
      </PanelEmpty>
    )
  } else {
    const first = page * ADK_RUN_REFUSALS_PAGE + 1
    const last = page * ADK_RUN_REFUSALS_PAGE + data.items.length
    body = (
      <>
        <div
          className={cn(
            "overflow-x-auto rounded-(--radius-card) border",
            list.isPlaceholderData && "opacity-60"
          )}
        >
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>When</TableHead>
                <TableHead>Workflow</TableHead>
                <TableHead>How, and who asked</TableHead>
                <TableHead>For whom</TableHead>
                <TableHead>Refused, and why</TableHead>
                <TableHead>From</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {data.items.map((refusal) => (
                <Refusal
                  key={refusal.id}
                  refusal={refusal}
                  workflowName={workflowNames.get(refusal.agent_id)}
                />
              ))}
            </TableBody>
          </Table>
        </div>
        <div className="mt-3 flex items-center gap-2 text-xs text-muted-foreground">
          <span className="tabular-nums">
            {first.toLocaleString()}–{last.toLocaleString()} of{" "}
            {data.total.toLocaleString()}
          </span>
          <Button
            variant="outline"
            size="xs"
            className="ml-auto"
            disabled={page === 0}
            onClick={() => onPage(page - 1)}
          >
            <Icon icon="left" data-icon="inline-start" />
            Newer
          </Button>
          <Button
            variant="outline"
            size="xs"
            disabled={last >= data.total}
            onClick={() => onPage(page + 1)}
          >
            Older
            <Icon icon="right" data-icon="inline-end" />
          </Button>
        </div>
      </>
    )
  }

  return (
    <div className="flex flex-col gap-4 px-4">
      <header className="flex flex-col gap-3">
        <div>
          <h2 className="text-sm font-[550]">Refused starts</h2>
          <p className="mt-0.5 max-w-[90ch] text-2xs text-muted-foreground">
            Every start of {organizationName}'s workflows that was refused, and
            made no run: someone who may not run them (without agents:run, or
            using an agent they may not), or what it'd run didn't fit or build.
            Each says how it came, for whom, why and from where.
          </p>
        </div>
        {filters}
      </header>
      {filtered && data && (
        <ActiveFilters className="mb-0" onClear={onClearFilters}>
          {data.total.toLocaleString()}{" "}
          {data.total === 1 ? "refused start matches" : "refused starts match"}
        </ActiveFilters>
      )}
      {body}
    </div>
  )
}
