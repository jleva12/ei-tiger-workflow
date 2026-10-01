import * as React from "react"
import {
  useMutation,
  useQuery,
  useQueryClient,
  type QueryClient,
} from "@tanstack/react-query"

import { ApiError } from "@/lib/api"
import { useMe } from "@/lib/users"
import type { ChatAgentDocument } from "./document"

/*
 * An organization's chat agents. Until the admin API keeps them (the
 * backend comes later), they're kept in this browser, per organization:
 * the same records and calls the API will have (a revision per save, a
 * 409 when it was saved since, a 404 when it's gone), through TanStack
 * Query like every resource, so moving them to the API is this file.
 */

export type ChatAgentRecord = {
  id: string
  organization_id: string
  /** Goes up by one with every save; a save names the one it was made from. */
  revision: number
  document: ChatAgentDocument
  created_at: string
  created_by: string
  updated_at: string
  updated_by: string
  /** Who saved it last, by name as they were then. */
  updated_by_name: string
}

/** Who's saving: their subject and name, as a record keeps them. */
export type Saver = { subject: string; name: string }

const keys = {
  all: ["chat-agents"] as const,
  list: (organizationId: string) => ["chat-agents", organizationId] as const,
  detail: (organizationId: string, id: string) =>
    ["chat-agents", organizationId, id] as const,
}

const storageKey = (organizationId: string) =>
  `forge.chat-agents.v1:${organizationId}`

function read(organizationId: string): ChatAgentRecord[] {
  try {
    const raw: unknown = JSON.parse(
      localStorage.getItem(storageKey(organizationId)) ?? "[]"
    )
    return Array.isArray(raw) ? (raw as ChatAgentRecord[]) : []
  } catch {
    return []
  }
}

function write(organizationId: string, records: ChatAgentRecord[]) {
  try {
    localStorage.setItem(storageKey(organizationId), JSON.stringify(records))
  } catch (caught) {
    throw new ApiError(
      "This browser couldn't keep the agent: its storage is full or blocked.",
      {
        kind: "unknown",
        cause: caught,
      }
    )
  }
}

const notFound = () =>
  new ApiError("The organization has no such agent", {
    kind: "http",
    status: 404,
  })

/** The organization's chat agents, as kept. */
export async function listChatAgents(organizationId: string) {
  return read(organizationId)
}

/** One of them; a 404 when it's gone. */
export async function getChatAgent(organizationId: string, id: string) {
  const record = read(organizationId).find((r) => r.id === id)
  if (!record) throw notFound()
  return record
}

/** Keeps a new chat agent under its document's ID, as its first revision. */
export async function createChatAgent(
  organizationId: string,
  document: ChatAgentDocument,
  by: Saver
): Promise<ChatAgentRecord> {
  const records = read(organizationId)
  const now = new Date().toISOString()
  const id = records.some((r) => r.id === document.id)
    ? `${document.id}_${records.length}`
    : document.id
  const record: ChatAgentRecord = {
    id,
    organization_id: organizationId,
    revision: 1,
    document: {
      ...document,
      id,
      organization_id: organizationId,
      created_at: now,
      updated_at: now,
    },
    created_at: now,
    created_by: by.subject,
    updated_at: now,
    updated_by: by.subject,
    updated_by_name: by.name,
  }
  write(organizationId, [...records, record])
  return record
}

/** Saves the next revision, made from `revision`: a 409 when it was saved since. */
export async function saveChatAgent(
  organizationId: string,
  id: string,
  { document, revision }: { document: ChatAgentDocument; revision: number },
  by: Saver
): Promise<ChatAgentRecord> {
  const records = read(organizationId)
  const index = records.findIndex((r) => r.id === id)
  if (index < 0) throw notFound()
  const kept = records[index]
  if (kept.revision !== revision) {
    throw new ApiError("Someone saved this agent since you opened it.", {
      kind: "http",
      status: 409,
    })
  }
  const now = new Date().toISOString()
  const record: ChatAgentRecord = {
    ...kept,
    revision: kept.revision + 1,
    document: {
      ...document,
      id,
      organization_id: organizationId,
      updated_at: now,
    },
    updated_at: now,
    updated_by: by.subject,
    updated_by_name: by.name,
  }
  write(
    organizationId,
    records.map((r, i) => (i === index ? record : r))
  )
  return record
}

/** Forgets one. */
export async function deleteChatAgent(organizationId: string, id: string) {
  const records = read(organizationId)
  if (!records.some((r) => r.id === id)) throw notFound()
  write(
    organizationId,
    records.filter((r) => r.id !== id)
  )
}

/** Puts a record into the cache, in its detail and in the organization's list. */
export function cacheChatAgent(client: QueryClient, record: ChatAgentRecord) {
  client.setQueryData(keys.detail(record.organization_id, record.id), record)
  client.setQueryData<ChatAgentRecord[]>(
    keys.list(record.organization_id),
    (list) =>
      list
        ? list.some((r) => r.id === record.id)
          ? list.map((r) => (r.id === record.id ? record : r))
          : [...list, record]
        : list
  )
}

const byRecent = (a: ChatAgentRecord, b: ChatAgentRecord) =>
  b.updated_at.localeCompare(a.updated_at)

/**
 * The organization's chat agents, most recently changed first: the
 * records, and their documents for what reads only those.
 */
export function useOrganizationChatAgents(organizationId: string) {
  const query = useQuery({
    queryKey: keys.list(organizationId),
    queryFn: () => listChatAgents(organizationId),
  })
  const records = React.useMemo(
    () => [...(query.data ?? [])].sort(byRecent),
    [query.data]
  )
  const agents = React.useMemo(() => records.map((r) => r.document), [records])
  return { ...query, records, agents }
}

/** One chat agent, read afresh (the builder saves from the revision it starts at). */
export function useChatAgent(organizationId: string, id: string | undefined) {
  return useQuery({
    queryKey: keys.detail(organizationId, id ?? ""),
    queryFn: () => getChatAgent(organizationId, id!),
    enabled: Boolean(id),
    refetchOnMount: "always",
    refetchOnWindowFocus: false,
    retry: false,
  })
}

/** Who's saving, as a record names them. */
export function useSaver(): Saver {
  const me = useMe()
  const user = me.data?.user
  const name = user
    ? `${user.first_name} ${user.last_name}`.trim() || user.email
    : undefined
  return React.useMemo(
    () => ({ subject: me.data?.subject ?? "me", name: name || "You" }),
    [me.data?.subject, name]
  )
}

/** Keeping a new chat agent (a new one, a copy, an import, the example). */
export function useCreateChatAgent(organizationId: string) {
  const client = useQueryClient()
  const by = useSaver()
  return useMutation({
    mutationFn: (document: ChatAgentDocument) =>
      createChatAgent(organizationId, document, by),
    onSuccess: (record) => cacheChatAgent(client, record),
    meta: { silent: true },
  })
}

/** Forgetting one. */
export function useDeleteChatAgent(organizationId: string) {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (id: string) => deleteChatAgent(organizationId, id),
    onSuccess: (_, id) => {
      client.removeQueries({ queryKey: keys.detail(organizationId, id) })
      client.setQueryData<ChatAgentRecord[]>(
        keys.list(organizationId),
        (list) => list?.filter((r) => r.id !== id)
      )
    },
    meta: { silent: true },
  })
}
