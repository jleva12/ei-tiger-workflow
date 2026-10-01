import * as React from "react"

import { ADMIN_TABLE_FEATURES } from "@/components/admin/table-config"
import {
  PrimaryAction,
  ToolbarFilters,
  ViewToolbar,
} from "@/components/forge/app-shell"
import {
  createColumnHelper,
  DataTable,
  type DataTableFeatureConfig,
} from "@/components/forge/data-table"
import {
  EmptyIllustration,
  EmptyWorkspace,
} from "@/components/forge/empty-state"
import { ErrorCallout } from "@/components/forge/feedback"
import { ShellHeaderActions, ShellToolbar } from "@/components/forge/shell"
import { Chip, ConnectionDot } from "@/components/forge/status"
import { ViewTabsList, ViewTabsTrigger } from "@/components/forge/toolbar"
import { Button } from "@/components/ui/button"
import { Tabs, TabsContent } from "@/components/ui/tabs"
import { formatRelative } from "@/lib/format"
import {
  EVENT_TYPE_STATUS,
  EVENTS_ICON,
  eventTypes,
  isEventsTab,
  useEventEndpoint,
  type EventEndpoint,
  type EventsTab,
  type EventType,
  type EventTypeStatus,
} from "@/lib/events"
import { useScopeAccess } from "@/lib/hierarchy"
import { parseTimestamp } from "@/lib/timestamps"
import { EndpointView } from "./endpoint-view"
import { ReceivedEvents } from "./received-events"

const propertyCount = (type: EventType) => {
  const properties = type.payload_schema.properties
  return properties && typeof properties === "object"
    ? Object.keys(properties).length
    : 0
}

/** A row of the table: one event type. */
type TypeRow = {
  id: string
  name: string
  key: string
  status: EventTypeStatus
  properties: number
  schema_version: number
  received: number
  invalid: number
  /** Empty when it never has. */
  last_received_at: string
}

const toRow = (type: EventType): TypeRow => ({
  id: type.id,
  name: type.name,
  key: type.key,
  status: type.status,
  properties: propertyCount(type),
  schema_version: type.schema_version,
  received: type.event_count,
  invalid: type.invalid_count,
  last_received_at: type.last_received_at ?? "",
})

// An organization has a handful of event types: sort, size, hide and export them.
const FEATURES: Partial<DataTableFeatureConfig> = {
  ...ADMIN_TABLE_FEATURES,
  globalFilter: false,
  pagination: false,
  faceting: false,
}

const helper = createColumnHelper<TypeRow>()
const COLUMNS = helper.columns([
  helper.accessor("name", {
    header: "Event type",
    // The column that stretches; this is its narrowest.
    size: 220,
    enableHiding: false,
    meta: { label: "Event type" },
    cell: ({ row: { original: row } }) => (
      <span className="flex min-w-0 flex-col">
        <span className="truncate text-[0.8125rem] font-medium text-foreground">
          {row.name}
        </span>
        <span className="truncate font-mono text-2xs text-muted-foreground">
          {row.key}
        </span>
      </span>
    ),
  }),
  helper.accessor("status", {
    header: "Status",
    size: 112,
    meta: { label: "Status" },
    cell: ({ getValue }) => {
      const { label, tone } = EVENT_TYPE_STATUS[getValue()]
      return <Chip tone={tone}>{label}</Chip>
    },
  }),
  helper.accessor("properties", {
    header: "Properties",
    size: 108,
    meta: {
      label: "Properties",
      align: "right",
      cellClassName: "text-muted-foreground",
    },
  }),
  helper.accessor("schema_version", {
    header: "Schema",
    size: 92,
    meta: {
      label: "Schema",
      align: "right",
      cellClassName: "text-muted-foreground",
    },
    cell: ({ getValue }) => `v${getValue()}`,
  }),
  helper.accessor("received", {
    header: "Received",
    size: 136,
    meta: { label: "Received", align: "right" },
    cell: ({ row: { original: row } }) => (
      <span className="tabular-nums">
        {row.received.toLocaleString()}
        {row.invalid > 0 && (
          <span className="ml-1.5 text-destructive">
            {row.invalid.toLocaleString()} invalid
          </span>
        )}
      </span>
    ),
  }),
  helper.accessor("last_received_at", {
    header: "Last received",
    size: 136,
    meta: { label: "Last received", cellClassName: "text-muted-foreground" },
    cell: ({ getValue }) => {
      const at = getValue()
      return at ? (
        <span title={parseTimestamp(at).toLocaleString()}>
          {formatRelative(at)}
        </span>
      ) : (
        <span className="text-subtle">Never</span>
      )
    },
  }),
])

/** A tab's count, quieter than its label. */
function TabCount({ value }: { value: number | undefined }) {
  if (value === undefined) return null
  return (
    <span className="text-2xs text-subtle tabular-nums">
      {value.toLocaleString()}
    </span>
  )
}

/** Whether events can arrive at all, in the toolbar on every tab. */
function EndpointStatus({
  endpoint,
  onOpen,
}: {
  /** Undefined while it loads; null before it's turned on. */
  endpoint: EventEndpoint | null | undefined
  onOpen: () => void
}) {
  if (endpoint === undefined) return null
  const on = endpoint?.enabled === true
  return (
    <Button
      variant="ghost"
      size="sm"
      onClick={onOpen}
      className="text-muted-foreground"
    >
      <ConnectionDot online={on} />
      {on
        ? "Endpoint receiving"
        : endpoint
          ? "Endpoint off"
          : "Endpoint not on yet"}
    </Button>
  )
}

/**
 * An organization's Events page, in three tabs: the event types it accepts (each
 * opening its own page with its schema), the events it has received, and
 * its inbound endpoint with how to send to it. The toolbar says on every
 * tab whether events can arrive; the top bar's Event type defines one.
 * Everyone in the organization reads it; whoever manages its events (its
 * admins) defines types and runs the endpoint.
 */
export function OrganizationEvents({
  organizationId,
  organizationName,
  tab,
  onTabChange,
  onOpenEventType,
}: {
  organizationId: string
  organizationName: string
  tab: EventsTab
  onTabChange: (tab: EventsTab) => void
  /** Open an event type's page; `new` to define one. */
  onOpenEventType: (id: string) => void
}) {
  const can = useScopeAccess(`org:${organizationId}`)
  const canManage = can("events:manage")
  const types = eventTypes.scope({ organizationId }).useList()
  const endpoint = useEventEndpoint(organizationId)
  const rows = React.useMemo(() => (types.data ?? []).map(toRow), [types.data])
  const received = types.data?.reduce((sum, t) => sum + t.event_count, 0)
  const exampleKey =
    types.data?.find((t) => t.status === "active")?.key ?? types.data?.[0]?.key
  const create = () => onOpenEventType("new")

  return (
    <Tabs
      value={tab}
      onValueChange={(value) => {
        if (isEventsTab(value)) onTabChange(value)
      }}
      className="gap-0"
    >
      {canManage && (
        <ShellHeaderActions>
          <PrimaryAction onClick={create}>Event type</PrimaryAction>
        </ShellHeaderActions>
      )}
      <ShellToolbar>
        <ViewToolbar>
          <ViewTabsList aria-label="Events">
            <ViewTabsTrigger value="types" icon={EVENTS_ICON}>
              Event types
              <TabCount value={types.data?.length} />
            </ViewTabsTrigger>
            <ViewTabsTrigger value="received" icon="activity">
              Received
              <TabCount value={received} />
            </ViewTabsTrigger>
            <ViewTabsTrigger value="endpoint" icon="link">
              Endpoint
            </ViewTabsTrigger>
          </ViewTabsList>
          <ToolbarFilters>
            <EndpointStatus
              endpoint={endpoint.data}
              onOpen={() => onTabChange("endpoint")}
            />
          </ToolbarFilters>
        </ViewToolbar>
      </ShellToolbar>

      <TabsContent value="types">
        {types.error ? (
          <ErrorCallout
            title="Couldn't load the event types"
            action={
              <Button
                variant="outline"
                size="sm"
                onClick={() => void types.refetch()}
              >
                Retry
              </Button>
            }
          >
            {types.error.message}
          </ErrorCallout>
        ) : types.data?.length === 0 ? (
          <EmptyWorkspace
            illustration={<EmptyIllustration name="waiting" />}
            title={`Let other systems tell ${organizationName} what happened`}
            description={
              canManage
                ? "Define the events you expect, such as incident.opened, with the schema each must match. Every event that arrives is kept and checked."
                : `${organizationName} accepts no events yet. Its admins define the event types.`
            }
            actions={
              canManage && (
                <Button variant="outline" onClick={create}>
                  New event type
                </Button>
              )
            }
            steps={[
              { icon: EVENTS_ICON, label: "Define an event type" },
              { icon: "review", label: "Review it, then turn it on" },
              { icon: "link", label: "Share the endpoint" },
            ]}
          />
        ) : (
          <DataTable
            className="rounded-none border-0"
            title="Event types"
            description={`The events ${organizationName} accepts. A draft or paused type refuses them; open one to edit its schema, try a payload or see what it received.`}
            columns={COLUMNS}
            data={rows}
            isLoading={!types.data}
            getRowId={(row) => row.id}
            features={FEATURES}
            stateKey="organization-event-types"
            exportFileName="event-types"
            labels={{ rows: "event types", rowSingular: "event type" }}
            onRowClick={(row) => onOpenEventType(row.original.id)}
          />
        )}
      </TabsContent>

      <TabsContent value="received">
        <ReceivedEvents organizationId={organizationId} types={types.data} />
      </TabsContent>

      <TabsContent value="endpoint">
        <EndpointView
          organizationId={organizationId}
          organizationName={organizationName}
          canManage={canManage}
          exampleKey={exampleKey}
        />
      </TabsContent>
    </Tabs>
  )
}
