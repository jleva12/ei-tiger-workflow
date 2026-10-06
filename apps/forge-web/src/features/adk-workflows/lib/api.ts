import * as React from "react"
import {
  useMutation,
  useQuery,
  useQueryClient,
  type QueryClient,
} from "@tanstack/react-query"

import type { VersionChoice } from "@/features/builder/components/version-field"
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
 *
 * A workflow has versions, as a chat agent does: the draft the builder
 * saves, and published versions nothing changes. Outside callers run
 * `ag_x` (its latest published), `ag_x@3`, or `ag_x@draft`.
 */

export type AgentRecord = {
  id: string
  organization_id: string
  /** Goes up by one with every save; a save names the one it was made from. */
  revision: number
  /** Its draft, or its latest published version when it has no draft. */
  document: AgentDocument
  /** draft (never published), published, or published+draft. */
  status: "draft" | "published" | "published+draft"
  has_draft: boolean
  /** The version the draft will be published as; null without a draft. */
  draft_version: number | null
  /** The latest published version, which `ag_…` runs; null before the first. */
  published_version: number | null
  published_at: string | null
  created_at: string
  created_by: string
  updated_at: string
  updated_by: string
  /** Who saved it last, by name as they were then. */
  updated_by_name: string | null
}

/** One published version. */
export type AgentVersion = {
  version: number
  published_at: string
  published_by: string
  published_by_name: string | null
}

/** A workflow with its published versions, newest first. */
export type AgentDetail = AgentRecord & { versions?: AgentVersion[] }

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
 * Save an agent's draft, made from `revision`. Rejects with an `ApiError`
 * (409 when someone saved it since, or `NO_DRAFT`: it's published). `keepalive`
 * lets the save finish after the page closes.
 */
export function saveAgent(
  organizationId: string,
  id: string,
  body: AgentSave,
  { keepalive = false } = {}
) {
  return api.put<AgentRecord, AgentSave>(
    `${agentPath(organizationId, id)}/draft`,
    body,
    {
      silent: true,
      ...(keepalive
        ? { adapter: "fetch", fetchOptions: { keepalive: true } }
        : {}),
    }
  )
}

/** Publishes the draft as its version: refused (422) with why it can't be yet. */
export function publishAgent(
  organizationId: string,
  id: string,
  revision: number
) {
  return api.post<AgentRecord, { revision: number }>(
    `${agentPath(organizationId, id)}/publish`,
    { revision },
    { silent: true }
  )
}

/** Starts the next version's draft, from a published version (the latest by default). */
export function startAgentVersion(
  organizationId: string,
  id: string,
  fromVersion?: number
) {
  return api.post<AgentRecord, { from_version?: number }>(
    `${agentPath(organizationId, id)}/versions`,
    fromVersion ? { from_version: fromVersion } : {},
    { silent: true }
  )
}

/** Discards the draft: the workflow is its latest published version again. */
export function discardAgentDraft(organizationId: string, id: string) {
  return api.delete<AgentRecord>(`${agentPath(organizationId, id)}/draft`, {
    silent: true,
  })
}

/** One published version, with its document. */
export function getAgentVersion(
  organizationId: string,
  id: string,
  version: number
) {
  return api.get<AgentVersion & { document: AgentDocument }>(
    `${agentPath(organizationId, id)}/versions/${version}`,
    { silent: true }
  )
}

/** One published version, to show read-only. */
export function useAgentVersion(
  organizationId: string,
  id: string,
  version: number | undefined
) {
  return useQuery({
    queryKey: ["organization-agent-version", organizationId, id, version ?? 0],
    queryFn: () => getAgentVersion(organizationId, id, version!),
    enabled: version !== undefined,
    staleTime: Infinity, // A published version never changes.
    retry: false,
  })
}

/** Publishing, starting a new version, discarding a draft: each answers the record. */
export function useAgentLifecycle(organizationId: string, id: string) {
  const client = useQueryClient()
  const done = (record: AgentRecord) => {
    cacheAgent(client, record)
    void client.invalidateQueries({
      queryKey: organizationAgents.scope({ organizationId }).keys.detail(id),
    })
  }
  const publish = useMutation({
    mutationFn: (revision: number) =>
      publishAgent(organizationId, id, revision),
    onSuccess: done,
    meta: { silent: true },
  })
  const newVersion = useMutation({
    mutationFn: (fromVersion?: number) =>
      startAgentVersion(organizationId, id, fromVersion),
    onSuccess: done,
    meta: { silent: true },
  })
  const discard = useMutation({
    mutationFn: () => discardAgentDraft(organizationId, id),
    onSuccess: done,
    meta: { silent: true },
  })
  return { publish, newVersion, discard }
}

/** Where each workflow is between draft and published, as version pickers offer it. */
export function versionChoices(
  records: AgentRecord[]
): Record<string, VersionChoice> {
  return Object.fromEntries(
    records.map((r) => [
      r.id,
      {
        published: r.published_version,
        hasDraft: r.has_draft,
        draftVersion: r.draft_version,
      },
    ])
  )
}
