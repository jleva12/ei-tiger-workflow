import {
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query"
import { WebhookIcon } from "@hugeicons/core-free-icons"

import { createNestedResource } from "@/lib/api/resource"
import { api } from "@/lib/api-instance"

/** The Events page's icon, in the sub nav and the header. */
export const EVENTS_ICON = WebhookIcon

/** The Events page's tabs; Event types is the default. */
export type EventsTab = "types" | "received" | "endpoint"

export const isEventsTab = (value: unknown): value is EventsTab =>
  value === "types" || value === "received" || value === "endpoint"

/** An event type page's tabs; Definition is the default. */
export type EventTypeTab = "definition" | "test" | "received" | "triggers"

export const isEventTypeTab = (value: unknown): value is EventTypeTab =>
  value === "definition" ||
  value === "test" ||
  value === "received" ||
  value === "triggers"

/**
 * Where an event type is: a draft until it's first turned on, then active
 * (the endpoint accepts it) or paused. Drafts and paused types are refused.
 */
export type EventTypeStatus = "draft" | "active" | "paused"

/** Each status as the console names it, with the chip tone it wears. */
export const EVENT_TYPE_STATUS: Record<
  EventTypeStatus,
  { label: string; tone: "notice" | "success" | "neutral" }
> = {
  draft: { label: "Draft", tone: "notice" },
  active: { label: "Accepting", tone: "success" },
  paused: { label: "Paused", tone: "neutral" },
}

/** What senders name an event type by, in the URL. */
export const EVENT_KEY_PATTERN = /^[a-z][a-z0-9_.-]{0,99}$/

type Audited = {
  created_at: string
  created_by: string
  updated_at: string
  updated_by: string
}

/**
 * An organization's inbound endpoint: where other systems post it events, at
 * `<url>/<event type key>` with its token.
 */
export type EventEndpoint = Audited & {
  id: string
  organization_id: string
  enabled: boolean
  /** The token's last four characters. */
  token_hint: string
  url: string
  /** Only in the answer that made the token; never again. */
  token: string | null
}

/** A kind of event the organization accepts, and the schema its payload must match. */
export type EventType = Audited & {
  id: string
  organization_id: string
  key: string
  name: string
  description: string
  status: EventTypeStatus
  payload_schema: Record<string, unknown>
  /** 1, then one more each time the schema checks differently. */
  schema_version: number
  event_count: number
  invalid_count: number
  last_received_at: string | null
}

export type EventTypeInput = {
  key: string
  name: string
  description: string
  payload_schema: Record<string, unknown>
}

/** A change: its definition, or turning it on or pausing it. */
export type EventTypeUpdate = Partial<EventTypeInput> & {
  status?: "active" | "paused"
}

/** Where and why a payload doesn't match its schema. */
export type EventIssue = {
  /** A JSONPath: `$`, `$.service.name`, `$.items[0]`. */
  path: string
  message: string
  /** The schema keyword it failed, e.g. `required`. */
  keyword: string
}

export type EventStatus = "valid" | "invalid"

/** An event the organization received, without its payload. */
export type ReceivedEvent = Audited & {
  id: string
  organization_id: string
  event_type_id: string
  event_key: string
  schema_version: number
  status: EventStatus
  errors: EventIssue[]
  idempotency_key: string | null
  size_bytes: number
  content_type: string
  user_agent: string
  source_ip: string
}

export type ReceivedEventDetail = ReceivedEvent & { payload: unknown }

export type EventFilters = {
  event_type_id?: string
  status?: EventStatus
}

/**
 * An organization's event types: list, define (as drafts), change, turn on or pause,
 * and delete (`events:manage`). Changing one refreshes the received events
 * too, which name its key.
 */
export const eventTypes = createNestedResource<
  EventType,
  { organizationId: string },
  { create: EventTypeInput; update: EventTypeUpdate }
>({
  api,
  key: "event-types",
  path: ({ organizationId }) => `/organizations/${organizationId}/event-types`,
  label: "event type",
})

const endpointKey = (organizationId: string) =>
  [...eventTypes.scope({ organizationId }).keys.all, "endpoint"] as const

/** The organization's endpoint; null before it's turned on the first time. */
export function useEventEndpoint(organizationId: string) {
  return useQuery({
    queryKey: endpointKey(organizationId),
    queryFn: ({ signal }) =>
      api.get<EventEndpoint | null>(
        `/organizations/${organizationId}/event-endpoint`,
        {
          signal,
        }
      ),
  })
}

/**
 * Turn the endpoint on or off; the first time on makes it, and its answer
 * carries the token. Rotating replaces the token, which the answer carries.
 */
export function useEndpointActions(organizationId: string) {
  const queryClient = useQueryClient()
  // Keep the token out of the cache: it's shown once, from the answer.
  const store = (endpoint: EventEndpoint) =>
    queryClient.setQueryData(endpointKey(organizationId), {
      ...endpoint,
      token: null,
    })
  const toggle = useMutation({
    mutationFn: (enabled: boolean) =>
      api.put<EventEndpoint>(
        `/organizations/${organizationId}/event-endpoint`,
        { enabled }
      ),
    meta: { errorTitle: "Couldn't change the endpoint" },
    onSuccess: store,
  })
  const rotate = useMutation({
    mutationFn: () =>
      api.post<EventEndpoint>(
        `/organizations/${organizationId}/event-endpoint/token`
      ),
    meta: { errorTitle: "Couldn't replace the token" },
    onSuccess: store,
  })
  return { toggle, rotate }
}

export const EVENTS_PAGE = 50

const eventsKey = (organizationId: string) =>
  [...eventTypes.scope({ organizationId }).keys.all, "received"] as const

// How often an open list of received events looks for new ones.
const RECEIVED_POLL_MS = 15_000

/**
 * The events the organization received, latest first, a page at a time; reread
 * every little while, since senders post them at any time.
 */
export function useReceivedEvents(
  organizationId: string,
  filters: EventFilters
) {
  return useInfiniteQuery({
    refetchInterval: RECEIVED_POLL_MS,
    queryKey: [...eventsKey(organizationId), filters],
    queryFn: ({ pageParam, signal }) =>
      api.get<ReceivedEvent[]>(`/organizations/${organizationId}/events`, {
        params: { ...filters, limit: EVENTS_PAGE, offset: pageParam },
        signal,
      }),
    initialPageParam: 0,
    getNextPageParam: (last, pages) =>
      last.length < EVENTS_PAGE ? undefined : pages.length * EVENTS_PAGE,
  })
}

/** One received event, with its payload. */
export function useReceivedEvent(
  organizationId: string,
  eventId: string | undefined
) {
  return useQuery({
    queryKey: [...eventsKey(organizationId), "detail", eventId],
    queryFn: ({ signal }) =>
      api.get<ReceivedEventDetail>(
        `/organizations/${organizationId}/events/${eventId}`,
        {
          signal,
        }
      ),
    enabled: eventId !== undefined,
  })
}

export type SchemaTrialResult = { valid: boolean; errors: EventIssue[] }

/** Check a sample payload against a schema, saved or not; stores nothing. */
export function useSchemaTrial(organizationId: string) {
  return useMutation({
    mutationFn: (input: {
      payload_schema: Record<string, unknown>
      payload: unknown
    }) =>
      api.post<SchemaTrialResult>(
        `/organizations/${organizationId}/event-schemas/validate`,
        input
      ),
    // The page shows why.
    meta: { silent: true },
  })
}

/** The curl command that sends an event, for senders to copy. */
export function curlExample(
  url: string,
  key: string,
  payload: unknown = {},
  token = "$FORGE_EVENTS_TOKEN"
) {
  const body = JSON.stringify(payload).replaceAll("'", "'\\''")
  return [
    `curl -X POST '${url}/${key}' \\`,
    `  -H "Authorization: Bearer ${token}" \\`,
    `  -H 'Content-Type: application/json' \\`,
    `  -H "Idempotency-Key: $(uuidgen)" \\`,
    `  -d '${body}'`,
  ].join("\n")
}
