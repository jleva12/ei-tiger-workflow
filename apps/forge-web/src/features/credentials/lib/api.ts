import {
  queryOptions,
  useMutation,
  useQuery,
  useQueryClient,
  type QueryClient,
} from "@tanstack/react-query"

import { createNestedResource } from "@/lib/api/resource"
import { api } from "@/lib/api-instance"
import type {
  AuthInput,
  AuthMethodInfo,
  McpServerRecord,
} from "@/features/mcp-servers/lib/api"

/*
 * An organization's API credentials, kept by the admin API (in MySQL): how
 * its workflows' HTTP steps and its agents' HTTP and OpenAPI tools sign in
 * to its APIs. Each has an auth method (`GET /credential-auth-methods` lists
 * them, with their forms' fields) and the URLs it may be sent to: a request
 * anywhere else goes without it. Its settings are answered as saved; its
 * secrets are write-only (the API says which are set, never what they are).
 * Reading needs `organizations:read`; everything else `credentials:manage`.
 */

export type CredentialRecord = {
  id: string
  organization_id: string
  name: string
  description: string
  /** Where it may be sent: URLs, each an origin and optionally a path. */
  urls: string[]
  auth: McpServerRecord["auth"]
  created_at: string
  created_by: string
  updated_at: string
  updated_by: string
}

export type CredentialCreate = {
  name: string
  description: string
  urls: string[]
  auth: AuthInput
}

export type CredentialUpdate = Partial<CredentialCreate>

export const organizationCredentials = createNestedResource<
  CredentialRecord,
  { organizationId: string },
  { create: CredentialCreate; update: CredentialUpdate }
>({
  api,
  key: "organization-credentials",
  path: ({ organizationId }) =>
    `/organizations/${encodeURIComponent(organizationId)}/credentials`,
  label: "Credential",
  updateMethod: "patch",
})

const credentialPath = (organizationId: string, id: string) =>
  `/organizations/${encodeURIComponent(organizationId)}/credentials/${encodeURIComponent(id)}`

/** The organization's API credentials, by name. */
export function useOrganizationCredentials(
  organizationId: string,
  { enabled = true } = {}
) {
  return organizationCredentials
    .scope({ organizationId })
    .useList(undefined, { enabled })
}

/** The ways a credential can sign in to an API. */
export function useCredentialAuthMethods() {
  return useQuery({
    queryKey: ["credential-auth-methods"],
    queryFn: ({ signal }) =>
      api.get<AuthMethodInfo[]>("/credential-auth-methods", { signal }),
    // They change only with the admin API.
    staleTime: Infinity,
  })
}

/** Puts a record the API answered with into the cache, detail and list. */
export function cacheCredential(client: QueryClient, record: CredentialRecord) {
  const scoped = organizationCredentials.scope({
    organizationId: record.organization_id,
  })
  client.setQueryData(scoped.keys.detail(record.id), record)
  client.setQueriesData<CredentialRecord[]>(
    { queryKey: scoped.keys.lists() },
    (list) =>
      list?.some((r) => r.id === record.id)
        ? list.map((r) => (r.id === record.id ? record : r))
        : list
          ? [...list, record].sort((a, b) => a.name.localeCompare(b.name))
          : list
  )
}

/** Forget the credential's sign-in. */
export function useDisconnectCredential(organizationId: string) {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (id: string) =>
      api.delete<CredentialRecord>(
        `${credentialPath(organizationId, id)}/oauth`
      ),
    meta: { errorTitle: "Couldn't sign out of the credential" },
    onSuccess: (record) => cacheCredential(client, record),
  })
}

/**
 * Begin signing in with the credential: answers where to send the browser.
 * It comes back to /oauth/credentials/callback.
 */
export function startCredentialOAuth(organizationId: string, id: string) {
  return api.post<{ authorization_url: string }>(
    `${credentialPath(organizationId, id)}/oauth/start`,
    undefined,
    { silent: true }
  )
}

/** Finish a sign-in with what the browser came back with. */
export function finishCredentialOAuth(params: {
  state: string
  code?: string
  error?: string
  error_description?: string
}) {
  return api.post<CredentialRecord>("/credential-oauth/callback", params, {
    silent: true,
  })
}

/* -------------------------------------------------------------------------- */
/* OpenAPI specs                                                              */
/* -------------------------------------------------------------------------- */

/** Where an OpenAPI tool's spec comes from, as its settings say. */
export type SpecSource = {
  source: "url" | "inline"
  url: string
  spec: string
  /** One of the organization's credentials: sent with the spec's request where it may go. */
  credential: string
}

/** One of a spec's operations: a tool the agent may call, by its tool's name. */
export type SpecOperation = {
  name: string
  operation_id: string
  method: string
  path: string
  summary: string
  description: string
}

/** A way the spec says its API takes credentials. */
export type SpecSecurity = {
  name: string
  type: string
  /** For apiKey: header, query or cookie, and its parameter. */
  location: string
  parameter: string
  /** For http: basic, bearer… */
  scheme: string
}

export type SpecSummary = {
  title: string
  version: string
  servers: string[]
  operations: SpecOperation[]
  security: SpecSecurity[]
}

/** Whether there's a spec to read: one pasted, or an http(s) URL. */
const specReady = (source: SpecSource) =>
  source.source === "inline"
    ? source.spec.trim().length > 0
    : /^https?:\/\/\S+$/.test(source.url.trim())

/** The query that reads a spec: the builders' operation lists and the instruction writer share it. */
export const openApiSpecQuery = (organizationId: string, source: SpecSource) =>
  queryOptions({
    queryKey: [
      "openapi-spec",
      organizationId,
      source.source,
      source.source === "inline" ? source.spec : source.url.trim(),
      source.credential,
    ],
    queryFn: ({ signal }) =>
      api.post<SpecSummary>(
        `/organizations/${encodeURIComponent(organizationId)}/openapi/operations`,
        source,
        { signal, silent: true }
      ),
    enabled: specReady(source) && Boolean(organizationId),
    // A spec rarely changes while it's being set up; a URL's is read again on focus.
    staleTime: 60_000,
    retry: false,
  })

/**
 * What an OpenAPI tool's spec offers, read by the admin API as the agent's
 * runtime reads it: its operations, named as the agent's tools are. Not
 * asked while there's nothing to read.
 */
export function useOpenApiSpec(organizationId: string, source: SpecSource) {
  return useQuery(openApiSpecQuery(organizationId, source))
}

/**
 * A spec's operations, from the cache when it's been read; undefined when
 * there's nothing to read or it can't be read.
 */
export async function openApiOperations(
  client: QueryClient,
  organizationId: string,
  source: SpecSource
): Promise<SpecOperation[] | undefined> {
  const query = openApiSpecQuery(organizationId, source)
  if (!query.enabled) return undefined
  try {
    return (await client.fetchQuery(query)).operations
  } catch {
    return undefined
  }
}
