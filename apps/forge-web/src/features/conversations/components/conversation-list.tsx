import * as React from "react"
import { cn } from "cn"

import { PanelEmpty } from "@/components/forge/empty-state"
import { ErrorCallout } from "@/components/forge/feedback"
import { Icon } from "@/components/forge/icon"
import { Chip } from "@/components/forge/status"
import { Button } from "@/components/ui/button"
import {
  InputGroup,
  InputGroupAddon,
  InputGroupInput,
} from "@/components/ui/input-group"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
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
  ASSISTANT_APP,
  CHANNELS,
  channelOf,
  formatCost,
  formatTokens,
  PAGE_SIZE,
  SINCE,
  useConversations,
  whoIs,
  type AuditScope,
  type Channel,
  type ConversationFilters,
  type ConversationRef,
  type ConversationSummary,
  type Person,
  type Since,
} from "@/features/conversations/lib/conversations"
import { formatDateTime, formatRelative } from "@/lib/format"

/** A conversation's channels, as chips. */
export function ChannelChips({ channels }: { channels: string[] }) {
  return (
    <span className="flex flex-wrap gap-1">
      {channels.map((channel) => {
        const shown = channelOf(channel)
        return (
          <Tooltip key={channel}>
            <TooltipTrigger render={<span />}>
              <Chip tone={shown.tone} className="h-5 px-1.5 text-2xs">
                {shown.label}
              </Chip>
            </TooltipTrigger>
            {shown.description && (
              <TooltipContent>{shown.description}</TooltipContent>
            )}
          </Tooltip>
        )
      })}
    </span>
  )
}

/** Who an ID is: a person's name and email, an API key's name, or the ID. */
export function Who({
  id,
  people,
  className,
}: {
  id: string | null | undefined
  people: Record<string, Person>
  className?: string
}) {
  const shown = whoIs(id, people)
  return (
    <span className={cn("flex min-w-0 flex-col", className)}>
      <span className="flex min-w-0 items-center gap-1.5 text-foreground">
        {shown.kind === "api_key" && (
          <Icon
            icon="link"
            size={12}
            className="shrink-0 text-muted-foreground"
          />
        )}
        <span className="truncate">{shown.label}</span>
      </span>
      {(shown.detail || shown.kind !== "other") && (
        <span className="truncate font-mono text-3xs text-subtle">
          {shown.detail ?? id}
        </span>
      )}
    </span>
  )
}

function useDebounced<T>(value: T, ms = 300) {
  const [settled, setSettled] = React.useState(value)
  React.useEffect(() => {
    const timer = window.setTimeout(() => setSettled(value), ms)
    return () => window.clearTimeout(timer)
  }, [value, ms])
  return settled
}

function Row({
  item,
  people,
  showAgent,
  onOpen,
}: {
  item: ConversationSummary
  people: Record<string, Person>
  showAgent: boolean
  onOpen: () => void
}) {
  const otherCallers = item.callers.filter((caller) => caller !== item.user_id)
  return (
    <TableRow
      tabIndex={0}
      onClick={onOpen}
      onKeyDown={(key) => {
        if (key.key === "Enter") onOpen()
      }}
      className="cursor-pointer"
    >
      <TableCell className="whitespace-nowrap">
        <Tooltip>
          <TooltipTrigger render={<span className="text-foreground" />}>
            {item.updated_at ? formatRelative(item.updated_at) : "—"}
          </TooltipTrigger>
          {item.updated_at && (
            <TooltipContent>
              Last active {formatDateTime(item.updated_at)}
              {item.created_at &&
                ` · started ${formatDateTime(item.created_at)}`}
            </TooltipContent>
          )}
        </Tooltip>
      </TableCell>
      {showAgent && (
        <TableCell className="max-w-48">
          <span className="flex min-w-0 items-center gap-1.5">
            <Icon
              icon={item.app_name === ASSISTANT_APP ? "sparkles" : "robot"}
              size={13}
              className="shrink-0 text-muted-foreground"
            />
            <span className="truncate">{item.agent_name}</span>
          </span>
        </TableCell>
      )}
      <TableCell className="max-w-60">
        <Who id={item.user_id} people={people} />
        {otherCallers.length > 0 && (
          <span className="mt-0.5 block truncate text-3xs text-muted-foreground">
            Called by{" "}
            {otherCallers
              .map((caller) => whoIs(caller, people).label)
              .join(", ")}
          </span>
        )}
      </TableCell>
      <TableCell>
        <ChannelChips channels={item.channels} />
      </TableCell>
      <TableCell className="text-right tabular-nums">
        {item.turns}
        {item.failed_turns > 0 && (
          <span className="ml-1 text-destructive">
            ({item.failed_turns} failed)
          </span>
        )}
      </TableCell>
      <TableCell className="text-right tabular-nums">
        {item.tool_calls}
      </TableCell>
      <TableCell className="text-right whitespace-nowrap tabular-nums">
        <Tooltip>
          <TooltipTrigger render={<span />}>
            {formatTokens(item.input_tokens + item.output_tokens)}
          </TooltipTrigger>
          <TooltipContent>
            {item.input_tokens.toLocaleString()} in ·{" "}
            {item.output_tokens.toLocaleString()} out · {item.model_calls} model
            calls
          </TooltipContent>
        </Tooltip>
      </TableCell>
      <TableCell className="text-right tabular-nums">
        {formatCost(item.cost)}
      </TableCell>
    </TableRow>
  )
}

/**
 * The conversations, the latest active first: found by who held or called
 * them (a name, an email, an API key's name, an ID), agent, channel and
 * when, each opening whole.
 */
export function ConversationList({
  scope,
  filters,
  onFiltersChange,
  onOpen,
}: {
  scope: AuditScope
  filters: ConversationFilters
  onFiltersChange: (filters: ConversationFilters) => void
  onOpen: (ref: ConversationRef) => void
}) {
  const [query, setQuery] = React.useState(filters.q ?? "")
  const settled = useDebounced(query)
  React.useEffect(() => {
    if ((filters.q ?? "") !== settled)
      onFiltersChange({ ...filters, q: settled || undefined })
    // Only the typed query settling changes the filters here.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [settled])
  // Another filter or search starts again from the first page.
  const key = JSON.stringify(filters)
  const [paging, setPaging] = React.useState({ key, page: 0 })
  const page = paging.key === key ? paging.page : 0
  const setPage = (next: number) => setPaging({ key, page: next })

  const list = useConversations(scope, filters, page)
  const data = list.data
  const site = !("organizationId" in scope)
  const showAgent = !site
  const agents = data?.agents ?? {}
  const agentOptions = [
    { value: "", label: "Every agent" },
    { value: "assistant", label: "Assistant" },
    ...Object.entries(agents).map(([id, name]) => ({ value: id, label: name })),
  ]
  const channelOptions = [
    { value: "", label: "Every channel" },
    ...(Object.keys(CHANNELS) as Channel[])
      .filter(
        (channel) => !site || channel === "assistant" || channel === "unknown"
      )
      .map((channel) => ({ value: channel, label: CHANNELS[channel].label })),
  ]
  const sinceOptions = (Object.keys(SINCE) as Since[]).map((value) => ({
    value,
    label: SINCE[value],
  }))
  const filtered = Boolean(
    filters.q ||
    filters.agent ||
    filters.channel ||
    (filters.since && filters.since !== "all")
  )

  let body: React.ReactNode
  if (list.error && !data) {
    body = (
      <ErrorCallout
        title={
          list.error.status === 403
            ? "You can't audit these conversations"
            : "Couldn't load the conversations"
        }
        action={
          list.error.status === 403 ? undefined : (
            <Button
              variant="outline"
              size="sm"
              onClick={() => void list.refetch()}
            >
              Retry
            </Button>
          )
        }
      >
        {list.error.message}
      </ErrorCallout>
    )
  } else if (!data) {
    body = (
      <div className="flex flex-col gap-2" aria-busy="true">
        {Array.from({ length: 6 }, (_, index) => (
          <Skeleton key={index} className="h-11 w-full" />
        ))}
      </div>
    )
  } else if (data.items.length === 0) {
    body = filtered ? (
      <PanelEmpty illustration="search">
        No conversation matches. Try another name, channel or time.
      </PanelEmpty>
    ) : (
      <PanelEmpty illustration="activity">
        {site
          ? "Nobody has talked to the assistant yet."
          : "Nobody has talked to this organization's agents or asked its assistant anything yet."}
      </PanelEmpty>
    )
  } else {
    const first = page * PAGE_SIZE + 1
    const last = page * PAGE_SIZE + data.items.length
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
                <TableHead>Last active</TableHead>
                {showAgent && <TableHead>Agent</TableHead>}
                <TableHead>User</TableHead>
                <TableHead>Channel</TableHead>
                <TableHead className="text-right">Turns</TableHead>
                <TableHead className="text-right">Tool calls</TableHead>
                <TableHead className="text-right">Tokens</TableHead>
                <TableHead className="text-right">Cost</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {data.items.map((item) => (
                <Row
                  key={`${item.app_name}/${item.user_id}/${item.session_id}`}
                  item={item}
                  people={data.people}
                  showAgent={showAgent}
                  onOpen={() =>
                    onOpen({
                      session: item.session_id,
                      app: item.app_name,
                      user: item.user_id,
                    })
                  }
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
            onClick={() => setPage(page - 1)}
          >
            <Icon icon="left" data-icon="inline-start" />
            Newer
          </Button>
          <Button
            variant="outline"
            size="xs"
            disabled={last >= data.total}
            onClick={() => setPage(page + 1)}
          >
            Older
            <Icon icon="right" data-icon="inline-end" />
          </Button>
        </div>
      </>
    )
  }

  return (
    <div className="flex flex-col gap-4">
      <p className="max-w-[90ch] text-[0.8125rem] text-muted-foreground">
        {site
          ? "Every conversation with the assistant, asked from inside an organization or not: each one's messages, tool calls, state and token usage."
          : "Every conversation with the organization's agents, from the builder's chat, the run API or A2A, and with the assistant asked from inside it: each one's messages, tool calls, state and token usage."}
      </p>
      <div className="flex flex-wrap items-center gap-2">
        <InputGroup className="h-8 w-[min(22rem,100%)]">
          <InputGroupInput
            aria-label="Find conversations by who held or called them"
            placeholder="Name, email, API key, user or session ID…"
            value={query}
            onChange={(change) => setQuery(change.target.value)}
            className="text-xs"
          />
          <InputGroupAddon align="inline-end">
            {list.isFetching && data ? (
              <Icon icon="loading" className="animate-spin" />
            ) : (
              <Icon icon="search" />
            )}
          </InputGroupAddon>
        </InputGroup>
        {showAgent && (
          <Select
            items={agentOptions}
            value={filters.agent ?? ""}
            onValueChange={(value) =>
              onFiltersChange({
                ...filters,
                agent: value ? String(value) : undefined,
              })
            }
          >
            <SelectTrigger
              size="sm"
              aria-label="Agent"
              className="max-w-56 text-xs"
            >
              <SelectValue />
            </SelectTrigger>
            <SelectContent alignItemWithTrigger={false}>
              {agentOptions.map((option) => (
                <SelectItem key={option.value} value={option.value}>
                  {option.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        )}
        <Select
          items={channelOptions}
          value={filters.channel ?? ""}
          onValueChange={(value) =>
            onFiltersChange({
              ...filters,
              channel: value ? (String(value) as Channel) : undefined,
            })
          }
        >
          <SelectTrigger size="sm" aria-label="Channel" className="text-xs">
            <SelectValue />
          </SelectTrigger>
          <SelectContent alignItemWithTrigger={false}>
            {channelOptions.map((option) => (
              <SelectItem key={option.value} value={option.value}>
                {option.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Select
          items={sinceOptions}
          value={filters.since ?? "all"}
          onValueChange={(value) =>
            onFiltersChange({
              ...filters,
              since:
                value && value !== "all" ? (String(value) as Since) : undefined,
            })
          }
        >
          <SelectTrigger
            size="sm"
            aria-label="Active since"
            className="text-xs"
          >
            <SelectValue />
          </SelectTrigger>
          <SelectContent alignItemWithTrigger={false}>
            {sinceOptions.map((option) => (
              <SelectItem key={option.value} value={option.value}>
                {option.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        {filtered && (
          <Button
            variant="ghost"
            size="sm"
            onClick={() => {
              setQuery("")
              onFiltersChange({})
            }}
          >
            Clear filters
          </Button>
        )}
      </div>
      {body}
    </div>
  )
}
