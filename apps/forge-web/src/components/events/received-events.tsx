import * as React from "react"
import { useQueryClient } from "@tanstack/react-query"

import { ADMIN_TABLE_FEATURES } from "@/components/admin/table-config"
import {
  createColumnHelper,
  DataTable,
  type DataTableFeatureConfig,
  type InitialTableState,
} from "@/components/forge/data-table"
import { PanelEmpty } from "@/components/forge/empty-state"
import { ErrorCallout, LoadMore } from "@/components/forge/feedback"
import { Icon } from "@/components/forge/icon"
import { Chip } from "@/components/forge/status"
import {
  DetailList,
  DetailRow,
  TaskSheetContent,
  TaskSheetHeader,
} from "@/components/forge/task-sheet"
import { Button } from "@/components/ui/button"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Sheet } from "@/components/ui/sheet"
import { Skeleton } from "@/components/ui/skeleton"
import { formatBytes, formatRelative } from "@/lib/format"
import {
  eventTypes,
  EVENTS_ICON,
  useReceivedEvent,
  useReceivedEvents,
  type EventIssue,
  type EventStatus,
  type EventType,
  type ReceivedEvent,
} from "@/lib/events"
import { parseTimestamp } from "@/lib/timestamps"
import { JsonView } from "./json-view"

type StatusFilter = EventStatus | "all"

const STATUS_ITEMS: { value: StatusFilter; label: string }[] = [
  { value: "all", label: "Any status" },
  { value: "valid", label: "Valid" },
  { value: "invalid", label: "Invalid" },
]

const fullTime = (iso: string) => parseTimestamp(iso).toLocaleString()

export function EventStatusChip({
  status,
  errors,
}: {
  status: EventStatus
  errors?: number
}) {
  return status === "valid" ? (
    <Chip tone="success" icon="check">
      Valid
    </Chip>
  ) : (
    <Chip tone="danger" icon="warning">
      Invalid{errors ? ` · ${errors}` : ""}
    </Chip>
  )
}

/** A row of the table: one received event. */
type EventRow = {
  id: string
  received_at: string
  event_key: string
  status: EventStatus
  idempotency_key: string
  size_bytes: number
  sender: string
  source_ip: string
  event: ReceivedEvent
}

const toRow = (event: ReceivedEvent): EventRow => ({
  id: event.id,
  received_at: event.created_at,
  event_key: event.event_key,
  status: event.status,
  idempotency_key: event.idempotency_key ?? "",
  size_bytes: event.size_bytes,
  sender: event.user_agent,
  source_ip: event.source_ip,
  event,
})

// The list pages on the server (Load more) and filters there, so the table
// neither pages nor searches what it has; it sorts, sizes, hides columns and
// exports.
const FEATURES: Partial<DataTableFeatureConfig> = {
  ...ADMIN_TABLE_FEATURES,
  globalFilter: false,
  pagination: false,
  faceting: false,
}

const helper = createColumnHelper<EventRow>()
const COLUMNS = helper.columns([
  helper.accessor("received_at", {
    header: "Received",
    size: 110,
    enableHiding: false,
    meta: { label: "Received" },
    cell: ({ getValue }) => (
      <span className="whitespace-nowrap" title={fullTime(getValue())}>
        {formatRelative(getValue())}
      </span>
    ),
  }),
  helper.accessor("event_key", {
    header: "Type",
    size: 136,
    meta: { label: "Type", cellClassName: "font-mono text-2xs" },
  }),
  helper.accessor("status", {
    header: "Status",
    size: 120,
    meta: { label: "Status" },
    cell: ({ row: { original: row } }) => (
      <EventStatusChip status={row.status} errors={row.event.errors.length} />
    ),
  }),
  helper.accessor("idempotency_key", {
    header: "Idempotency key",
    size: 130,
    meta: {
      label: "Idempotency key",
      cellClassName: "font-mono text-2xs text-muted-foreground",
    },
    cell: ({ getValue }) => getValue() || "—",
  }),
  helper.accessor("size_bytes", {
    header: "Size",
    size: 76,
    meta: {
      label: "Size",
      align: "right",
      cellClassName: "text-muted-foreground",
    },
    cell: ({ getValue }) => formatBytes(getValue()),
  }),
  helper.accessor("sender", {
    header: "Sender",
    size: 130,
    meta: { label: "Sender", cellClassName: "text-muted-foreground" },
    cell: ({ getValue }) => getValue() || "—",
  }),
  helper.accessor("source_ip", {
    header: "From",
    size: 120,
    meta: {
      label: "From",
      cellClassName: "font-mono text-2xs text-muted-foreground",
    },
    cell: ({ getValue }) => getValue() || "—",
  }),
])

// Where it came from is in the Columns menu; on an event type's page, so is
// its type.
const ORGANIZATION_STATE: InitialTableState = {
  columnVisibility: { source_ip: false },
}
const TYPE_STATE: InitialTableState = {
  columnVisibility: { source_ip: false, event_key: false },
}

/**
 * The events the organization received, latest first, valid or not; each opens with
 * its payload and, when it didn't match, where. On the Events page it lists
 * every type's, with a filter; on an event type's page, only that type's.
 */
export function ReceivedEvents({
  organizationId,
  types,
  eventTypeId,
}: {
  organizationId: string
  /** The organization's event types, to filter by (without `eventTypeId`). */
  types?: EventType[]
  /** Only this event type's. */
  eventTypeId?: string
}) {
  const [status, setStatus] = React.useState<StatusFilter>("all")
  const [typeFilter, setTypeFilter] = React.useState("all")
  const [opened, setOpened] = React.useState<ReceivedEvent>()
  const type = eventTypeId ?? (typeFilter === "all" ? undefined : typeFilter)
  const received = useReceivedEvents(organizationId, {
    event_type_id: type,
    status: status === "all" ? undefined : status,
  })
  const events = React.useMemo(
    () => received.data?.pages.flat(),
    [received.data]
  )
  const rows = React.useMemo(() => (events ?? []).map(toRow), [events])
  // New events change the event types' counts: reread them.
  const queryClient = useQueryClient()
  const newest = events?.[0]?.id
  React.useEffect(() => {
    if (newest === undefined) return
    const keys = eventTypes.scope({ organizationId }).keys
    void queryClient.invalidateQueries({ queryKey: keys.lists() })
    void queryClient.invalidateQueries({ queryKey: keys.details() })
  }, [newest, queryClient, organizationId])
  const filtered = status !== "all" || (!eventTypeId && typeFilter !== "all")
  const typeItems = React.useMemo(
    () => [
      { value: "all", label: "Every type" },
      ...(types ?? []).map((t) => ({ value: t.id, label: t.name })),
    ],
    [types]
  )

  const filters = (
    <div className="flex items-center gap-1.5">
      {!eventTypeId && types && types.length > 1 && (
        <Select
          items={typeItems}
          value={typeFilter}
          onValueChange={(value) => setTypeFilter(value ?? "all")}
        >
          <SelectTrigger size="sm" aria-label="Event type" className="min-w-36">
            <SelectValue />
          </SelectTrigger>
          <SelectContent alignItemWithTrigger={false}>
            {typeItems.map((item) => (
              <SelectItem key={item.value} value={item.value}>
                {item.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      )}
      <Select
        items={STATUS_ITEMS}
        value={status}
        onValueChange={(value) => setStatus((value as StatusFilter) ?? "all")}
      >
        <SelectTrigger size="sm" aria-label="Status" className="min-w-32">
          <SelectValue />
        </SelectTrigger>
        <SelectContent alignItemWithTrigger={false}>
          {STATUS_ITEMS.map((item) => (
            <SelectItem key={item.value} value={item.value}>
              {item.label}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      <Button
        variant="ghost"
        size="icon-sm"
        aria-label="Check for new events"
        disabled={received.isFetching}
        onClick={() => void received.refetch()}
      >
        <Icon icon="refresh" />
      </Button>
    </div>
  )

  return (
    <section className="flex flex-col gap-3">
      {received.error && !events ? (
        <ErrorCallout
          title="Couldn't load the events"
          action={
            <Button
              variant="outline"
              size="sm"
              onClick={() => void received.refetch()}
            >
              Retry
            </Button>
          }
        >
          {received.error.message}
        </ErrorCallout>
      ) : (
        // On the Events page it's titled and borderless, like the event
        // types; an event type's own tab names it there. The table says how
        // many, and filters.
        <DataTable
          className={eventTypeId ? undefined : "rounded-none border-0"}
          title={eventTypeId ? undefined : "Received events"}
          description={
            events
              ? `${eventTypeId ? "" : "Every event sent to the organization's endpoint · "}${events.length.toLocaleString()}${received.hasNextPage ? "+" : ""} ${events.length === 1 ? "event" : "events"}, latest first`
              : undefined
          }
          toolbarActions={filters}
          columns={COLUMNS}
          data={rows}
          isLoading={!events}
          getRowId={(row) => row.id}
          features={FEATURES}
          initialState={eventTypeId ? TYPE_STATE : ORGANIZATION_STATE}
          stateKey={
            eventTypeId ? "event-type-received" : "organization-received-events"
          }
          exportFileName="events"
          labels={{ rows: "events", rowSingular: "event" }}
          onRowClick={(row) => setOpened(row.original.event)}
          emptyState={
            <PanelEmpty illustration={filtered ? "search" : "waiting"}>
              {filtered
                ? "No events match these filters."
                : "Nothing received yet. Events show here as senders post them."}
            </PanelEmpty>
          }
        />
      )}
      {received.hasNextPage && (
        <LoadMore
          label="Load more events"
          size="sm"
          disabled={received.isFetchingNextPage}
          onClick={() => void received.fetchNextPage()}
        />
      )}

      <EventSheet
        organizationId={organizationId}
        event={opened}
        onClose={() => setOpened(undefined)}
      />
    </section>
  )
}

/** One received event: what it was, where it failed, and its payload. */
function EventSheet({
  organizationId,
  event,
  onClose,
}: {
  organizationId: string
  event: ReceivedEvent | undefined
  onClose: () => void
}) {
  // Kept while the sheet closes.
  const [shown, setShown] = React.useState(event)
  if (event && event !== shown) setShown(event)
  const detail = useReceivedEvent(organizationId, event?.id)

  return (
    <Sheet
      open={event !== undefined}
      onOpenChange={(open) => !open && onClose()}
    >
      <TaskSheetContent>
        {shown && (
          <>
            <TaskSheetHeader
              eyebrow={shown.event_key}
              eyebrowIcon={EVENTS_ICON}
              title={`Received ${formatRelative(shown.created_at)}`}
              description={
                shown.status === "valid"
                  ? `It matched version ${shown.schema_version} of the schema.`
                  : `It didn't match version ${shown.schema_version} of the schema. It's kept, but won't set anything off.`
              }
            />
            <DetailList>
              <DetailRow label="Status">
                <EventStatusChip
                  status={shown.status}
                  errors={shown.errors.length}
                />
              </DetailRow>
              <DetailRow label="Received">
                {fullTime(shown.created_at)}
              </DetailRow>
              <DetailRow label="Idempotency key">
                <span className="font-mono break-all">
                  {shown.idempotency_key ?? "None sent"}
                </span>
              </DetailRow>
              <DetailRow label="Size">
                {formatBytes(shown.size_bytes)}
              </DetailRow>
              <DetailRow label="Content type">
                {shown.content_type || "—"}
              </DetailRow>
              <DetailRow label="Sender">{shown.user_agent || "—"}</DetailRow>
              <DetailRow label="From">
                <span className="font-mono">{shown.source_ip || "—"}</span>
              </DetailRow>
              <DetailRow label="Event ID">
                <span className="font-mono break-all">{shown.id}</span>
              </DetailRow>
            </DetailList>
            <div className="flex flex-col gap-5 px-7 py-6 max-[600px]:px-[22px]">
              {shown.errors.length > 0 && <IssueList issues={shown.errors} />}
              <div className="flex flex-col gap-2">
                <h3 className="text-xs font-medium">Payload</h3>
                {detail.error ? (
                  <ErrorCallout title="Couldn't load the payload">
                    {detail.error.message}
                  </ErrorCallout>
                ) : detail.data?.id === shown.id ? (
                  <JsonView
                    value={detail.data.payload}
                    className="max-h-[28rem]"
                  />
                ) : (
                  <Skeleton className="h-40 w-full" />
                )}
              </div>
            </div>
          </>
        )}
      </TaskSheetContent>
    </Sheet>
  )
}

/** Where and why a payload didn't match. */
export function IssueList({ issues }: { issues: EventIssue[] }) {
  return (
    <div className="flex flex-col gap-2">
      <h3 className="text-xs font-medium">
        Didn't match the schema ({issues.length})
      </h3>
      <ul className="flex flex-col divide-y divide-danger-border overflow-hidden rounded-(--radius-control) border border-danger-border bg-danger-surface">
        {issues.map((issue, index) => (
          <li
            key={`${issue.path}-${index}`}
            className="flex flex-col gap-0.5 px-3 py-2 text-xs text-danger-foreground"
          >
            <span className="flex items-center gap-2">
              <code className="font-mono font-medium">{issue.path}</code>
              {issue.keyword && (
                <span className="text-3xs tracking-wide uppercase opacity-75">
                  {issue.keyword}
                </span>
              )}
            </span>
            <span className="wrap-anywhere">{issue.message}</span>
          </li>
        ))}
      </ul>
    </div>
  )
}
