import * as React from "react"
import type { QueryClient } from "@tanstack/react-query"

import { createNestedResource } from "@/lib/api/resource"
import { api } from "@/lib/api-instance"
import type { AgentDocument } from "./document"

/*
 * An organization's agents, kept by the admin API (in MongoDB) and shared by
 * the whole organization: every member reads the same ones, and saving needs
 * `agents:manage` (its admins and members have it). A record wraps the
 * builder's document with its scope, its revision and who saved it when,
 * as a workflow's does. The API sets the document's `id`, `organization_id`
 * and times; the builder sends the rest as it is.
 */

export type AgentRecord = {
  id: string
  organization_id: string
  /** Goes up by one with every save; a save names the one it was made from. */
  revision: number
  document: AgentDocument
  created_at: string
  created_by: string
  updated_at: string
  updated_by: string
  /** Who saved it last, by name as they were then. */
  updated_by_name: string
}

/** A new agent: the API makes its ID. */
export type AgentCreate = { document: AgentDocument }

export type AgentSave = { document: AgentDocument; revision: number }

export const organizationAgents = createNestedResource<
  AgentRecord,
  { organizationId: string },
  { create: AgentCreate; update: AgentSave }
>({
  api,
  key: "organization-agents",
  path: ({ organizationId }) =>
    `/organizations/${encodeURIComponent(organizationId)}/agents`,
  label: "Workflow",
  updateMethod: "put",
})

export const agentPath = (organizationId: string, id: string) =>
  `/organizations/${encodeURIComponent(organizationId)}/agents/${encodeURIComponent(id)}`

const byRecent = (a: AgentRecord, b: AgentRecord) =>
  b.updated_at.localeCompare(a.updated_at)

/**
 * The organization's agents, most recently changed first: the records, and
 * their documents for what reads only those.
 */
export function useOrganizationAgents(
  organizationId: string,
  { enabled = true } = {}
) {
  const query = organizationAgents
    .scope({ organizationId })
    .useList(undefined, { enabled })
  const records = React.useMemo(
    () => [...(query.data ?? [])].sort(byRecent),
    [query.data]
  )
  const agents = React.useMemo(() => records.map((r) => r.document), [records])
  return { ...query, records, agents }
}

/**
 * Puts a record the API answered with into the cache, in its detail and in
 * the organization's list, so both show it without asking again.
 */
export function cacheAgent(client: QueryClient, record: AgentRecord) {
  const scoped = organizationAgents.scope({
    organizationId: record.organization_id,
  })
  client.setQueryData(scoped.keys.detail(record.id), record)
  client.setQueriesData<AgentRecord[]>(
    { queryKey: scoped.keys.lists() },
    (list) => {
      if (!list) return list
      return list.some((r) => r.id === record.id)
        ? list.map((r) => (r.id === record.id ? record : r))
        : [record, ...list]
    }
  )
}

/**
 * Save an agent's next version, made from `revision`. Rejects with an
 * `ApiError` (409 when someone saved it since). `keepalive` lets the save
 * finish after the page closes.
 */
export function saveAgent(
  organizationId: string,
  id: string,
  body: AgentSave,
  { keepalive = false } = {}
) {
  return api.put<AgentRecord, AgentSave>(agentPath(organizationId, id), body, {
    silent: true,
    ...(keepalive
      ? { adapter: "fetch", fetchOptions: { keepalive: true } }
      : {}),
  })
}
