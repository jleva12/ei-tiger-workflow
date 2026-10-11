import {
  DatabaseIcon,
  FlashIcon,
  Globe02Icon,
  GroupItemsIcon,
  HardDriveIcon,
  HierarchyIcon,
  LaptopIcon,
  QueueIcon,
  ServerStack01Icon,
} from "@hugeicons/core-free-icons"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"

import type { IconProp } from "@/components/forge/icons"
import { createNestedResource } from "@/lib/api/resource"
import { api } from "@/lib/api-instance"

/*
 * A system design knowledge base: its applications (some of the
 * organization's code repositories, `./repositories`), the other parts of
 * the system people add by hand (components: databases, queues, external
 * systems…, `…/knowledge-bases/:kb/components`), the groups drawn around
 * them (`…/groups`), how they all connect, and the applications' code
 * graphs. Connections are the edges of its system map, drawn by hand
 * (`…/connections`); the code links of one between two applications say
 * where in each one's code graph it happens, and are written into the code
 * graph, whose queries (and agents) follow them across. Where everything is
 * on the map, and which group each application and component is in, is the
 * map's (`…/map`). Agents searching the knowledge base are told the whole
 * design. Reading takes `organizations:read`; drawing and removing
 * `knowledge_bases:manage`.
 */

export const SYSTEM_ICON: IconProp = HierarchyIcon

/**
 * How one part of the system connects to another: unspecified, calling its
 * API, using it as a library, sending it events, or sharing a database or
 * storage with it; reading from it, writing to it or both (a database, a
 * cache, storage); publishing to it or consuming from it (a queue).
 */
export type ConnectionKind =
  | "connects_to"
  | "calls"
  | "depends_on"
  | "events"
  | "shares_data"
  | "reads_from"
  | "writes_to"
  | "reads_writes"
  | "publishes_to"
  | "consumes_from"

/** Each kind as the map labels its edges, with what it means. */
export const CONNECTION_KINDS: Record<
  ConnectionKind,
  { label: string; description: string }
> = {
  connects_to: {
    label: "Connects to",
    description: "Connected, without saying how",
  },
  calls: { label: "Calls", description: "Calls its API: HTTP, gRPC, GraphQL" },
  depends_on: {
    label: "Depends on",
    description: "Uses it as a library or package",
  },
  events: {
    label: "Sends events to",
    description: "Publishes events or messages it consumes",
  },
  shares_data: {
    label: "Shares data with",
    description: "Reads or writes the same database or storage",
  },
  reads_from: {
    label: "Reads from",
    description: "Reads its data: a database, a cache, storage",
  },
  writes_to: {
    label: "Writes to",
    description: "Writes its data: a database, a cache, storage",
  },
  reads_writes: {
    label: "Reads and writes",
    description: "Reads and writes its data: a database, a cache, storage",
  },
  publishes_to: {
    label: "Publishes to",
    description: "Publishes messages or events to it: a queue or topic",
  },
  consumes_from: {
    label: "Consumes from",
    description: "Consumes its messages or events: a queue or topic",
  },
}

export const CONNECTION_KIND_ITEMS = (
  Object.keys(CONNECTION_KINDS) as ConnectionKind[]
).map((kind) => ({ value: kind, label: CONNECTION_KINDS[kind].label }))

/**
 * A connection from one of the knowledge base's applications or components
 * to another: each end is an application (a repository) or a component.
 */
export type Connection = {
  id: string
  knowledge_base_id: string
  /** The application (code repository) it's from, e.g. the caller; null from a component. */
  source_repository_id: string | null
  /** The application it's to, e.g. the one called; null to a component. */
  target_repository_id: string | null
  /** The component it's from, when it isn't from an application. */
  source_component_id: string | null
  /** The component it's to, when it isn't to an application. */
  target_component_id: string | null
  kind: ConnectionKind
  /** A note on it, e.g. which API; may be empty. */
  description: string
  /** How many code links say where it happens. */
  code_links: number
  created_at: string
  created_by: string
  updated_at: string
  updated_by: string
}

/** Each end by its repository's ID or its component's, not both. */
export type ConnectionCreate = {
  source_repository_id?: string
  source_component_id?: string
  target_repository_id?: string
  target_component_id?: string
  kind: ConnectionKind
  description: string
}

export type ConnectionUpdate = Partial<
  Pick<ConnectionCreate, "kind" | "description">
>

/** Something on the map a connection can join: an application or a component. */
export type MapEnd = { type: "application" | "component"; id: string }

/** A connection's end: its source's or its target's. */
export function endOf(
  connection: Connection,
  side: "source" | "target"
): MapEnd {
  const repository = connection[`${side}_repository_id`]
  return repository
    ? { type: "application", id: repository }
    : { type: "component", id: connection[`${side}_component_id`] ?? "" }
}

/** Whether a connection joins an end, either way. */
export function touches(connection: Connection, id: string) {
  return (
    endOf(connection, "source").id === id ||
    endOf(connection, "target").id === id
  )
}

/** The body naming a connection's end. */
export function endFields(
  side: "source" | "target",
  end: MapEnd
): Partial<ConnectionCreate> {
  return end.type === "application"
    ? { [`${side}_repository_id`]: end.id }
    : { [`${side}_component_id`]: end.id }
}

type Scope = { organizationId: string; knowledgeBaseId: string }

const connectionsPath = ({ organizationId, knowledgeBaseId }: Scope) =>
  `/organizations/${encodeURIComponent(organizationId)}/knowledge-bases/${encodeURIComponent(knowledgeBaseId)}/connections`

/**
 * The knowledge base's connections: list, draw, change and remove. Removing
 * an application from the knowledge base removes its connections.
 */
export const connections = createNestedResource<
  Connection,
  Scope,
  { create: ConnectionCreate; update: ConnectionUpdate }
>({
  api,
  key: "knowledge-base-connections",
  path: connectionsPath,
  label: "connection",
  updateMethod: "patch",
})

/** One end of a code link: a node of an application's code graph. */
export type CodeEnd = {
  node_id: string
  kind: string
  name: string
  qualified_name: string
  /** Its file, as the graph has it. */
  path: string
}

/**
 * Where a connection happens in the code: a declaration in each
 * application's code graph, such as the method that calls an API and the
 * handler that serves it.
 */
export type CodeLink = {
  id: string
  connection_id: string
  source: CodeEnd
  target: CodeEnd
  /** What connects them, e.g. POST /v1/orders or a topic. */
  label: string
  created_at: string
  created_by: string
  updated_at: string
  updated_by: string
}

export type CodeLinkCreate = {
  source_node_id: string
  target_node_id: string
  label: string
}

/**
 * A connection's code links: list, add and remove. Adding or removing one
 * changes the connection's count, so callers refresh the connections too.
 */
export const codeLinks = createNestedResource<
  CodeLink,
  Scope & { connectionId: string },
  { create: CodeLinkCreate }
>({
  api,
  key: "knowledge-base-code-links",
  path: ({ connectionId, ...scope }) =>
    `${connectionsPath(scope)}/${encodeURIComponent(connectionId)}/code-links`,
  label: "code link",
})

/**
 * What a component is: a database, a queue or topic, a cache, file or
 * object storage, a system outside the organization, a service without a
 * code repository here, or a client people use.
 */
export type ComponentKind =
  "database" | "queue" | "cache" | "storage" | "external" | "service" | "client"

/**
 * Each kind with its icon, what it means, an example of what it runs on,
 * and the connection usually drawn to one.
 */
export const COMPONENT_KINDS: Record<
  ComponentKind,
  {
    label: string
    description: string
    icon: IconProp
    technology: string
    connection: ConnectionKind
  }
> = {
  database: {
    label: "Database",
    description: "Where data is stored and queried",
    icon: DatabaseIcon,
    technology: "PostgreSQL 16, DynamoDB, MongoDB",
    connection: "reads_writes",
  },
  queue: {
    label: "Queue or topic",
    description: "Carries messages or events between parts",
    icon: QueueIcon,
    technology: "Kafka topic orders.v1, SQS, RabbitMQ",
    connection: "publishes_to",
  },
  cache: {
    label: "Cache",
    description: "Keeps data close for fast reads",
    icon: FlashIcon,
    technology: "Redis, Memcached",
    connection: "reads_writes",
  },
  storage: {
    label: "Storage",
    description: "Files and objects: a bucket, a file share",
    icon: HardDriveIcon,
    technology: "S3 bucket invoices, GCS",
    connection: "writes_to",
  },
  external: {
    label: "External system",
    description: "Outside the organization: a provider, a partner's API",
    icon: Globe02Icon,
    technology: "Stripe API, Salesforce",
    connection: "calls",
  },
  service: {
    label: "Service",
    description: "A service without a code repository here",
    icon: ServerStack01Icon,
    technology: "Keycloak, a legacy SOAP service",
    connection: "calls",
  },
  client: {
    label: "Client",
    description: "What people use: a browser, a mobile app",
    icon: LaptopIcon,
    technology: "iOS app, web browser",
    connection: "calls",
  },
}

export const COMPONENT_KIND_ITEMS = (
  Object.keys(COMPONENT_KINDS) as ComponentKind[]
).map((kind) => ({ value: kind, label: COMPONENT_KINDS[kind].label }))

export const GROUP_ICON: IconProp = GroupItemsIcon

/**
 * A part of the system that isn't one of the knowledge base's applications:
 * nothing ingests it, so people add it on the map. Which group it's in is
 * the map's.
 */
export type Component = {
  id: string
  knowledge_base_id: string
  kind: ComponentKind
  /** Unique in the knowledge base, e.g. orders-db. */
  name: string
  /** What it runs on, e.g. PostgreSQL 16; may be empty. */
  technology: string
  /** What it holds or does; may be empty. */
  description: string
  created_at: string
  created_by: string
  updated_at: string
  updated_by: string
}

export type ComponentCreate = Pick<
  Component,
  "kind" | "name" | "technology" | "description"
> & {
  /** A group to put it in as it's added. */
  group_id?: string
}

export type ComponentUpdate = Partial<
  Pick<Component, "kind" | "name" | "technology" | "description">
>

/**
 * A named part of the system drawn around some of its applications and
 * components: a domain, a team's services, a network. What's in it is the
 * map's.
 */
export type Group = {
  id: string
  knowledge_base_id: string
  /** Unique in the knowledge base. */
  name: string
  /** What it is and what it's for; agents read it. */
  description: string
  created_at: string
  created_by: string
  updated_at: string
  updated_by: string
}

export type GroupCreate = Pick<Group, "name" | "description">

const basePath = (scope: Scope) =>
  connectionsPath(scope).replace(/\/connections$/, "")

/**
 * The knowledge base's components: list, add, change and remove. Removing
 * one removes its connections, and adding one into a group moves it on the
 * map, so callers refresh those too (`useDesignRefresh`).
 */
export const components = createNestedResource<
  Component,
  Scope,
  { create: ComponentCreate; update: ComponentUpdate }
>({
  api,
  key: "knowledge-base-components",
  path: (scope) => `${basePath(scope)}/components`,
  label: "component",
  updateMethod: "patch",
})

/**
 * The knowledge base's groups: list, add, change and remove. Removing one
 * leaves what was in it on the map, in no group.
 */
export const groups = createNestedResource<
  Group,
  Scope,
  { create: GroupCreate; update: Partial<GroupCreate> }
>({
  api,
  key: "knowledge-base-groups",
  path: (scope) => `${basePath(scope)}/groups`,
  label: "group",
  updateMethod: "patch",
})

/** How a system map arranges what's on it: force-directed, in layers, or in a circle. */
export type MapLayout = "cose" | "breadthfirst" | "circle"

export type MapPoint = { x: number; y: number }

/**
 * A system map as people left it, the same for everyone: each placed
 * application's, component's and group's place by its ID (an application's
 * is its repository's), the group each application and component is in,
 * and the layout last picked (null until it's arranged).
 */
export type SystemMapLayout = {
  layout: MapLayout | null
  positions: Record<string, MapPoint>
  groups: Record<string, string>
}

/**
 * Places for some of what's on the map (the others keep theirs), groups for
 * some applications and components (null takes one out of its group), or a
 * layout.
 */
export type SystemMapUpdate = {
  layout?: MapLayout
  positions?: Record<string, MapPoint>
  groups?: Record<string, string | null>
}

const mapPath = (scope: Scope) =>
  connectionsPath(scope).replace(/\/connections$/, "/map")

const mapKey = (scope: Scope) =>
  ["knowledge-base-map", scope.organizationId, scope.knowledgeBaseId] as const

/** The knowledge base's system map as people left it. */
export function useSystemMapLayout(scope: Scope) {
  return useQuery({
    queryKey: mapKey(scope),
    queryFn: ({ signal }) =>
      api.get<SystemMapLayout>(mapPath(scope), { signal }),
  })
}

/**
 * Saves places on the map, groups, or its layout (`knowledge_bases:manage`).
 * The answer, the whole saved map, replaces the cached one.
 */
export function useSaveSystemMapLayout(scope: Scope) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (update: SystemMapUpdate) =>
      api.patch<SystemMapLayout>(mapPath(scope), update),
    meta: { errorTitle: "Couldn't save the map" },
    onSuccess: (saved) => queryClient.setQueryData(mapKey(scope), saved),
  })
}

/**
 * Refreshes what a change to a component or a group changes beside it:
 * the connections (a component's go with it) and the map (a group's
 * members leave it; a component added into a group joins it).
 */
export function useDesignRefresh(scope: Scope) {
  const queryClient = useQueryClient()
  return () =>
    Promise.all([
      queryClient.invalidateQueries({ queryKey: connections.keys.all }),
      queryClient.invalidateQueries({ queryKey: mapKey(scope) }),
    ])
}
