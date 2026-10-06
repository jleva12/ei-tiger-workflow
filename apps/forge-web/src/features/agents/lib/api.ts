import * as React from "react"
import {
  useMutation,
  useQuery,
  useQueryClient,
  type QueryClient,
} from "@tanstack/react-query"

import { api } from "@/lib/api-instance"
import { ApiError } from "@/lib/api/index"
import { useMe } from "@/lib/users"
import type { ChatAgentDocument } from "./document"

/*
 * An organization's chat agents, kept by the admin API (in MongoDB) and
 * shared by the whole organization. An agent has one ID for life and goes
 * through versions:
 *
 * - its **draft**, which the builder edits and autosaves (a save names the
 *   revision it was made from: a 409 when someone saved it since);
 * - **published** versions, which never change: publishing freezes the
 *   draft as its version, and changing a published agent means starting a
 *   new version's draft.
 *
 * A record's `document` is what the builder shows: the draft, or the latest
 * published version when there's no draft. The runtime runs `ca_x` (the
 * latest published), `ca_x@3` or `ca_x@draft` (`/runtime`).
 */

export type ChatAgentStatus = "draft" | "published" | "published+draft"

export type ChatAgentRecord = {
  id: string
  organization_id: string
  /** Goes up with every change; a draft save names the one it was made from. */
  revision: number
  document: ChatAgentDocument
  status: ChatAgentStatus
  has_draft: boolean
  /** The version the draft will be published as; null without a draft. */
  draft_version: number | null
  /** The latest published version; null before the first. */
  published_version: number | null
  published_at: string | null
  created_at: string
  created_by: string
  updated_at: string
  updated_by: string
  /** Who changed it last, by name as they were then. */
  updated_by_name: string | null
}

export type ChatAgentVersion = {
  version: number
  published_at: string
  published_by: string
  published_by_name: string | null
}

export type ChatAgentDetail = ChatAgentRecord & {
  /** Its published versions, newest first. */
  versions: ChatAgentVersion[]
}

/** Who's saving: their subject and name. The API records them itself now. */
export type Saver = { subject: string; name: string }

const keys = {
  all: ["chat-agents"] as const,
  list: (organizationId: string) => ["chat-agents", organizationId] as const,
  detail: (organizationId: string, id: string) =>
    ["chat-agents", organizationId, id] as const,
  version: (organizationId: string, id: string, version: number) =>
    ["chat-agents", organizationId, id, "versions", version] as const,
}

const base = (organizationId: string) =>
  `/organizations/${encodeURIComponent(organizationId)}/chat-agents`
const one = (organizationId: string, id: string) =>
  `${base(organizationId)}/${encodeURIComponent(id)}`

/** The organization's chat agents. */
export function listChatAgents(organizationId: string) {
  return api.get<ChatAgentRecord[]>(base(organizationId), { silent: true })
}

/** One of them, with its published versions; a 404 when it's gone. */
export function getChatAgent(organizationId: string, id: string) {
  return api.get<ChatAgentDetail>(one(organizationId, id), { silent: true })
}

/** A new chat agent: a draft of version 1, with an ID the API makes. */
export function createChatAgent(
  organizationId: string,
  document: ChatAgentDocument
): Promise<ChatAgentRecord> {
  return api.post<ChatAgentRecord, { document: ChatAgentDocument }>(
    base(organizationId),
    { document },
    { silent: true }
  )
}

/**
 * Saves the draft, made from `revision`: a 409 when it was saved since, or
 * (`NO_DRAFT`) when it's published with no draft. `keepalive` lets the save
 * finish after the page closes.
 */
export function saveChatAgent(
  organizationId: string,
  id: string,
  body: { document: ChatAgentDocument; revision: number },
  { keepalive = false } = {}
): Promise<ChatAgentRecord> {
  return api.put<ChatAgentRecord, typeof body>(
    `${one(organizationId, id)}/draft`,
    body,
    {
      silent: true,
      ...(keepalive
        ? { adapter: "fetch", fetchOptions: { keepalive: true } }
        : {}),
    }
  )
}

/** Publishes the draft (at `revision`) as its version; a 422 says why it can't run. */
export function publishChatAgent(
  organizationId: string,
  id: string,
  revision: number
) {
  return api.post<ChatAgentRecord, { revision: number }>(
    `${one(organizationId, id)}/publish`,
    { revision },
    { silent: true }
  )
}

/** Starts the next version's draft, from a published version (the latest by default). */
export function startChatAgentVersion(
  organizationId: string,
  id: string,
  fromVersion?: number
) {
  return api.post<ChatAgentRecord, { from_version?: number }>(
    `${one(organizationId, id)}/versions`,
    fromVersion ? { from_version: fromVersion } : {},
    { silent: true }
  )
}

/** Discards the draft: the agent is its latest published version again. */
export function discardChatAgentDraft(organizationId: string, id: string) {
  return api.delete<ChatAgentRecord>(`${one(organizationId, id)}/draft`, {
    silent: true,
  })
}

/** One published version, with its document. */
export function getChatAgentVersion(
  organizationId: string,
  id: string,
  version: number
) {
  return api.get<ChatAgentVersion & { document: ChatAgentDocument }>(
    `${one(organizationId, id)}/versions/${version}`,
    { silent: true }
  )
}

/** A published version as a file to run anywhere: the saved agents it uses bundled in. */
export function exportChatAgentVersion(
  organizationId: string,
  id: string,
  version: number
) {
  return api.get<ChatAgentDocument & { version: number }>(
    `${one(organizationId, id)}/versions/${version}/export`,
    { silent: true }
  )
}

/** What a standalone project of an agent holds, before it's downloaded. */
/**
 * What a standalone project is made of (forge-agent-runtime's StarterOptions):
 * where it keeps conversations, files and memories, how it replies, whether it
 * has the chat UI, whether it speaks A2A too, who may call it, and who owns it.
 */
export type StarterOptions = {
  interface: "ui" | "api"
  sessions: "memory" | "sqlite" | "postgresql" | "mysql"
  artifacts: "memory" | "folder" | "s3"
  memory: "memory" | "atlas"
  streaming: boolean
  /** Google's A2A protocol beside ADK's run API: /a2a and its agent card. */
  a2a: boolean
  api_key: boolean
  cors_origins: string[]
  code_owners: string[]
}

export const DEFAULT_STARTER_OPTIONS: StarterOptions = {
  interface: "ui",
  sessions: "memory",
  artifacts: "memory",
  memory: "memory",
  streaming: true,
  a2a: true,
  api_key: false,
  cors_origins: [],
  code_owners: [],
}

/** Which version (or the draft), its name, and what it's made of. */
export type StandaloneRequest = {
  version: number | "draft"
  name?: string
  options: StarterOptions
}

export type StandalonePreview = {
  name: string
  version: number | "draft"
  /** Its files' paths. */
  files: string[]
  /** What only Forge runs, what can't be bundled, and that it's a draft. */
  notes: string[]
  /** Where it gets the runtime: wheels (in vendor/) or pypi:<version>. */
  runtime: string
  /** What its .env needs filled in: the models' keys, the tools' secrets, API keys. */
  env: string[]
}

/** What a standalone project of a version (or the draft) would hold. */
export function previewStandalone(
  organizationId: string,
  id: string,
  body: StandaloneRequest
) {
  return api.post<StandalonePreview, StandaloneRequest>(
    `${one(organizationId, id)}/standalone/preview`,
    body,
    { silent: true }
  )
}

/** The project itself: a zip with the server, the UI and the agent. */
export function downloadStandalone(
  organizationId: string,
  id: string,
  body: StandaloneRequest
) {
  return api.post<Blob, StandaloneRequest>(
    `${one(organizationId, id)}/standalone`,
    body,
    { responseType: "blob", silent: true }
  )
}

/** A new agent (an ID of its own) whose draft is a copy of this one. */
export function duplicateChatAgent(
  organizationId: string,
  id: string,
  name?: string
) {
  return api.post<ChatAgentRecord, { name?: string }>(
    `${one(organizationId, id)}/duplicate`,
    name ? { name } : {},
    { silent: true }
  )
}

/** Deletes one: its versions stop running. */
export function deleteChatAgent(organizationId: string, id: string) {
  return api.delete<void>(one(organizationId, id), { silent: true })
}

/** The code a 409 carries (`NO_DRAFT`, `DRAFT_EXISTS`, `NOT_PUBLISHED`), if any. */
export function conflictCode(error: unknown): string | undefined {
  if (!(error instanceof ApiError) || error.status !== 409) return undefined
  const detail = (error.data as { detail?: unknown } | undefined)?.detail
  return detail && typeof detail === "object" && "code" in detail
    ? String((detail as { code: unknown }).code)
    : undefined
}

/** Why a draft can't be published (a 422's problems), if that's what it is. */
export function publishProblems(error: unknown): string[] {
  if (!(error instanceof ApiError) || error.status !== 422) return []
  const detail = (error.data as { detail?: unknown } | undefined)?.detail
  if (detail && typeof detail === "object" && "problems" in detail) {
    const problems = (detail as { problems: unknown }).problems
    return Array.isArray(problems) ? problems.map(String) : []
  }
  return typeof detail === "string" ? [detail] : []
}

/** Puts a record into the cache, in its detail and in the organization's list. */
export function cacheChatAgent(client: QueryClient, record: ChatAgentRecord) {
  client.setQueryData<ChatAgentDetail>(
    keys.detail(record.organization_id, record.id),
    (kept) => ({ versions: kept?.versions ?? [], ...record })
  )
  client.setQueryData<ChatAgentRecord[]>(
    keys.list(record.organization_id),
    (list) =>
      list
        ? list.some((r) => r.id === record.id)
          ? list.map((r) => (r.id === record.id ? record : r))
          : [record, ...list]
        : list
  )
}

const byRecent = (a: ChatAgentRecord, b: ChatAgentRecord) =>
  b.updated_at.localeCompare(a.updated_at)

/**
 * The organization's chat agents, most recently changed first: the
 * records, and their documents for what reads only those.
 */
export function useOrganizationChatAgents(
  organizationId: string,
  { enabled = true } = {}
) {
  const query = useQuery({
    queryKey: keys.list(organizationId),
    queryFn: () => listChatAgents(organizationId),
    enabled,
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

/** One published version, to show read-only. */
export function useChatAgentVersion(
  organizationId: string,
  id: string,
  version: number | undefined
) {
  return useQuery({
    queryKey: keys.version(organizationId, id, version ?? 0),
    queryFn: () => getChatAgentVersion(organizationId, id, version!),
    enabled: version !== undefined,
    staleTime: Infinity, // A published version never changes.
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

/** Keeping a new chat agent (a new one, an import, the example, one moved from this browser). */
export function useCreateChatAgent(organizationId: string) {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (document: ChatAgentDocument) =>
      createChatAgent(organizationId, document),
    onSuccess: (record) => cacheChatAgent(client, record),
    meta: { silent: true },
  })
}

/** Copying one into a new agent. */
export function useDuplicateChatAgent(organizationId: string) {
  const client = useQueryClient()
  return useMutation({
    mutationFn: ({ id, name }: { id: string; name?: string }) =>
      duplicateChatAgent(organizationId, id, name),
    onSuccess: (record) => cacheChatAgent(client, record),
    meta: { silent: true },
  })
}

/** Publishing, starting a new version, discarding a draft: each answers the record. */
export function useChatAgentLifecycle(organizationId: string, id: string) {
  const client = useQueryClient()
  const done = (record: ChatAgentRecord) => {
    cacheChatAgent(client, record)
    void client.invalidateQueries({ queryKey: keys.detail(organizationId, id) })
  }
  const publish = useMutation({
    mutationFn: (revision: number) =>
      publishChatAgent(organizationId, id, revision),
    onSuccess: done,
    meta: { silent: true },
  })
  const newVersion = useMutation({
    mutationFn: (fromVersion?: number) =>
      startChatAgentVersion(organizationId, id, fromVersion),
    onSuccess: done,
    meta: { silent: true },
  })
  const discard = useMutation({
    mutationFn: () => discardChatAgentDraft(organizationId, id),
    onSuccess: done,
    meta: { silent: true },
  })
  return { publish, newVersion, discard }
}

/** Deleting one. */
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

/* -------------------------------------------------------------------------- */
/* Agents kept in this browser before the API kept them                       */
/* -------------------------------------------------------------------------- */

const localKey = (organizationId: string) =>
  `forge.chat-agents.v1:${organizationId}`

/** The agents this browser kept for the organization before the API did. */
export function localChatAgents(organizationId: string): ChatAgentDocument[] {
  try {
    const raw: unknown = JSON.parse(
      localStorage.getItem(localKey(organizationId)) ?? "[]"
    )
    return Array.isArray(raw)
      ? raw
          .map(
            (record) => (record as { document?: ChatAgentDocument }).document
          )
          .filter((doc): doc is ChatAgentDocument => Boolean(doc))
      : []
  } catch {
    return []
  }
}

/** Forgets them, once they've moved. */
export function forgetLocalChatAgents(organizationId: string) {
  try {
    localStorage.removeItem(localKey(organizationId))
  } catch {
    // Nothing to forget.
  }
}

// Where the hosted runtime is: shared with the workflows' (lib/runtime).
export { a2aCardAddress, runtimeAddress, RUNTIME_URL } from "@/lib/runtime"
