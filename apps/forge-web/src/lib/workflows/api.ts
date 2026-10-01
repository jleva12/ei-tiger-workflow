import * as React from "react"
import { useQueryClient, type QueryClient } from "@tanstack/react-query"

import { toApiError } from "@/lib/api"
import { createNestedResource } from "@/lib/api/resource"
import { api } from "@/lib/api-instance"
import type { WorkflowDocument } from "./document"

/*
 * An organization's workflows, kept by the admin API (in MongoDB) and shared by
 * the whole organization: every member reads the same ones, and saving needs
 * `workflows:manage` (its admins and members have it). A record wraps the
 * builder's document with its scope, its revision and who saved it when.
 * The API sets the document's `id`, `organization_id` and times; the builder sends
 * the rest as it is.
 */

export type WorkflowRecord = {
  id: string
  organization_id: string
  /** Goes up by one with every save; a save names the one it was made from. */
  revision: number
  document: WorkflowDocument
  created_at: string
  created_by: string
  updated_at: string
  updated_by: string
  /** Who saved it last, by name as they were then. */
  updated_by_name: string
}

/** A new workflow: the API makes its ID. */
export type WorkflowCreate = { document: WorkflowDocument }

export type WorkflowSave = { document: WorkflowDocument; revision: number }

export const organizationWorkflows = createNestedResource<
  WorkflowRecord,
  { organizationId: string },
  { create: WorkflowCreate; update: WorkflowSave }
>({
  api,
  key: "organization-workflows",
  path: ({ organizationId }) => `/organizations/${encodeURIComponent(organizationId)}/workflows`,
  label: "workflow",
  updateMethod: "put",
})

export const workflowPath = (organizationId: string, id: string) =>
  `/organizations/${encodeURIComponent(organizationId)}/workflows/${encodeURIComponent(id)}`

const byRecent = (a: WorkflowRecord, b: WorkflowRecord) =>
  b.updated_at.localeCompare(a.updated_at)

/**
 * The organization's workflows, most recently changed first: the records, and
 * their documents for what reads only those.
 */
export function useOrganizationWorkflows(organizationId: string, { enabled = true } = {}) {
  const query = organizationWorkflows.scope({ organizationId }).useList(undefined, { enabled })
  const records = React.useMemo(() => [...(query.data ?? [])].sort(byRecent), [query.data])
  const workflows = React.useMemo(() => records.map((r) => r.document), [records])
  return { ...query, records, workflows }
}

/**
 * Puts a record the API answered with into the cache, in its detail and in
 * the organization's list, so both show it without asking again.
 */
export function cacheWorkflow(client: QueryClient, record: WorkflowRecord) {
  const scoped = organizationWorkflows.scope({ organizationId: record.organization_id })
  client.setQueryData(scoped.keys.detail(record.id), record)
  client.setQueriesData<WorkflowRecord[]>({ queryKey: scoped.keys.lists() }, (list) => {
    if (!list) return list
    return list.some((r) => r.id === record.id)
      ? list.map((r) => (r.id === record.id ? record : r))
      : [record, ...list]
  })
}

/**
 * Save a workflow's next version, made from `revision`. Rejects with an
 * `ApiError` (409 when someone saved it since). `keepalive` lets the save
 * finish after the page closes.
 */
export function saveWorkflow(
  organizationId: string,
  id: string,
  body: WorkflowSave,
  { keepalive = false } = {}
) {
  return api.put<WorkflowRecord, WorkflowSave>(workflowPath(organizationId, id), body, {
    silent: true,
    ...(keepalive ? { adapter: "fetch", fetchOptions: { keepalive: true } } : {}),
  })
}

export type SaveManyResult = { ok: true } | { ok: false; error: string }

/**
 * Save several of the organization's workflows at once (links to an event type),
 * each from the revision the list holds. What saves is kept; the first
 * failure is reported.
 */
export function useSaveWorkflows(organizationId: string) {
  const client = useQueryClient()
  return React.useCallback(
    async (changes: { record: WorkflowRecord; document: WorkflowDocument }[]) => {
      const results = await Promise.allSettled(
        changes.map(({ record, document }) =>
          saveWorkflow(organizationId, record.id, { document, revision: record.revision })
        )
      )
      let failure: string | undefined
      for (const result of results) {
        if (result.status === "fulfilled") cacheWorkflow(client, result.value)
        else failure ??= toApiError(result.reason).message
      }
      if (failure) {
        void client.invalidateQueries({ queryKey: organizationWorkflows.scope({ organizationId }).keys.all })
        return { ok: false, error: failure } satisfies SaveManyResult
      }
      return { ok: true } satisfies SaveManyResult
    },
    [client, organizationId]
  )
}
