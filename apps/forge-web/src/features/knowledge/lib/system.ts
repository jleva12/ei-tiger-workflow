import { HierarchyIcon } from "@hugeicons/core-free-icons"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"

import type { IconProp } from "@/components/forge/icons"
import { createNestedResource } from "@/lib/api/resource"
import { api } from "@/lib/api-instance"

/*
 * A system design knowledge base: its applications (some of the
 * organization's code repositories, `./repositories`), how they connect,
 * and their code graphs. Connections are the edges of its system map, drawn
 * by hand (`…/knowledge-bases/:kb/connections`); their code links say where
 * in each application's code graph a connection happens, and are written
 * into the code graph, whose queries (and agents) follow them across.
 * Reading takes `organizations:read`; drawing and removing
 * `knowledge_bases:manage`.
 */

export const SYSTEM_ICON: IconProp = HierarchyIcon

/**
 * How one application connects to another: unspecified, calling its API,
 * using it as a library, sending it events, or sharing a database or
 * storage with it.
 */
export type ConnectionKind =
  "connects_to" | "calls" | "depends_on" | "events" | "shares_data"

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
}

export const CONNECTION_KIND_ITEMS = (
  Object.keys(CONNECTION_KINDS) as ConnectionKind[]
).map((kind) => ({ value: kind, label: CONNECTION_KINDS[kind].label }))

/** A connection from one of the knowledge base's applications to another. */
export type Connection = {
  id: string
  knowledge_base_id: string
  /** The application (code repository) it's from, e.g. the caller. */
  source_repository_id: string
  /** The application it's to, e.g. the one called. */
  target_repository_id: string
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

export type ConnectionCreate = {
  source_repository_id: string
  target_repository_id: string
  kind: ConnectionKind
  description: string
}

export type ConnectionUpdate = Partial<
  Pick<ConnectionCreate, "kind" | "description">
>

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

/** How a system map arranges applications: force-directed, in layers, or in a circle. */
export type MapLayout = "cose" | "breadthfirst" | "circle"

export type MapPoint = { x: number; y: number }

/**
 * A system map as people left it, the same for everyone: each placed
 * application's place by repository ID, and the layout last picked (null
 * until it's arranged).
 */
export type SystemMapLayout = {
  layout: MapLayout | null
  positions: Record<string, MapPoint>
}

/** Places for some applications (the others keep theirs), or a layout. */
export type SystemMapUpdate = {
  layout?: MapLayout
  positions: Record<string, MapPoint>
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
 * Saves applications' places on the map, or its layout
 * (`knowledge_bases:manage`). The answer, the whole saved map, replaces the
 * cached one.
 */
export function useSaveSystemMapLayout(scope: Scope) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (update: SystemMapUpdate) =>
      api.patch<SystemMapLayout>(mapPath(scope), update),
    meta: { errorTitle: "Couldn't save the map's layout" },
    onSuccess: (saved) => queryClient.setQueryData(mapKey(scope), saved),
  })
}
