import {
  useMutation,
  useQuery,
  useQueryClient,
  type QueryClient,
} from "@tanstack/react-query"

import { createNestedResource } from "@/lib/api/resource"
import { api } from "@/lib/api-instance"

/*
 * An organization's MCP servers, kept by the admin API (in MySQL): remote
 * MCP servers, over streamable HTTP, its agents use as toolsets. Each has an
 * auth method (`GET /mcp-auth-methods` lists them, with their forms'
 * fields): its settings are answered as saved; its secrets are write-only
 * (the API says which are set, never what they are). Reading needs
 * `organizations:read`; everything else `mcp_servers:manage`.
 */

export type HeaderRow = { name: string; value: string }

/** One field of an auth method's form. */
export type AuthField = {
  name: string
  label: string
  description: string
  /** Write-only: kept encrypted and never answered. */
  secret: boolean
  required: boolean
  default: string
  placeholder: string
}

export type AuthMethodInfo = {
  kind: string
  label: string
  description: string
  /** A person connects the server (signs in) before agents use it. */
  interactive: boolean
  fields: AuthField[]
}

export type McpServerStatus = "unchecked" | "ok" | "error" | "needs_auth"

export type McpTool = { name: string; title: string | null; description: string }

export type McpServerRecord = {
  id: string
  organization_id: string
  name: string
  description: string
  url: string
  transport: "streamable_http"
  headers: HeaderRow[]
  timeout_seconds: number
  auth: {
    kind: string
    settings: Record<string, string>
    /** Which of its secrets are saved. */
    secrets_set: string[]
    interactive: boolean
    /** For a method a person connects: whether someone has. */
    connected: boolean | null
    connected_by: string | null
    connected_by_name: string | null
    connected_at: string | null
    expires_at: string | null
    scope: string | null
  }
  /** What the last check found. */
  status: McpServerStatus
  /** Its tools, when last checked. */
  tools: McpTool[]
  server_info: { name?: string; version?: string } | null
  checked_at: string | null
  last_error: string | null
  created_at: string
  created_by: string
  updated_at: string
  updated_by: string
}

export type AuthInput = {
  kind: string
  settings: Record<string, string>
  /** A value replaces the saved secret, null clears it; one left out stays. */
  secrets: Record<string, string | null>
}

export type McpServerCreate = {
  name: string
  description: string
  url: string
  headers: HeaderRow[]
  timeout_seconds: number
  auth: AuthInput
}

export type McpServerUpdate = Partial<McpServerCreate>

export const organizationMcpServers = createNestedResource<
  McpServerRecord,
  { organizationId: string },
  { create: McpServerCreate; update: McpServerUpdate }
>({
  api,
  key: "organization-mcp-servers",
  path: ({ organizationId }) =>
    `/organizations/${encodeURIComponent(organizationId)}/mcp-servers`,
  label: "MCP server",
  updateMethod: "patch",
})

const serverPath = (organizationId: string, id: string) =>
  `/organizations/${encodeURIComponent(organizationId)}/mcp-servers/${encodeURIComponent(id)}`

/** The organization's MCP servers, by name. */
export function useOrganizationMcpServers(
  organizationId: string,
  { enabled = true } = {}
) {
  return organizationMcpServers
    .scope({ organizationId })
    .useList(undefined, { enabled })
}

/** The ways the admin API can authenticate to an MCP server. */
export function useAuthMethods() {
  return useQuery({
    queryKey: ["mcp-auth-methods"],
    queryFn: ({ signal }) =>
      api.get<AuthMethodInfo[]>("/mcp-auth-methods", { signal }),
    // They change only with the admin API.
    staleTime: Infinity,
  })
}

/** Puts a record the API answered with into the cache, detail and list. */
export function cacheMcpServer(client: QueryClient, record: McpServerRecord) {
  const scoped = organizationMcpServers.scope({
    organizationId: record.organization_id,
  })
  client.setQueryData(scoped.keys.detail(record.id), record)
  client.setQueriesData<McpServerRecord[]>(
    { queryKey: scoped.keys.lists() },
    (list) =>
      list?.some((r) => r.id === record.id)
        ? list.map((r) => (r.id === record.id ? record : r))
        : list
  )
}

/**
 * Connect to the server as its agents would and list its tools. What it
 * found comes back on the record (a server that can't be reached is still
 * an answer, with `status: "error"`).
 */
export function useCheckMcpServer(organizationId: string) {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (id: string) =>
      api.post<McpServerRecord>(`${serverPath(organizationId, id)}/check`),
    meta: { errorTitle: "Couldn't check the MCP server" },
    onSuccess: (record) => cacheMcpServer(client, record),
  })
}

/** Forget the server's sign-in. */
export function useDisconnectMcpServer(organizationId: string) {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (id: string) =>
      api.delete<McpServerRecord>(`${serverPath(organizationId, id)}/oauth`),
    meta: { errorTitle: "Couldn't disconnect the MCP server" },
    onSuccess: (record) => cacheMcpServer(client, record),
  })
}

/**
 * Begin signing in to the server's authorization server: answers where to
 * send the browser. It comes back to /oauth/mcp/callback.
 */
export function startMcpOAuth(organizationId: string, id: string) {
  return api.post<{ authorization_url: string }>(
    `${serverPath(organizationId, id)}/oauth/start`,
    undefined,
    { silent: true }
  )
}

/** Finish a sign-in with what the browser came back with. */
export function finishMcpOAuth(params: {
  state: string
  code?: string
  error?: string
  error_description?: string
}) {
  return api.post<McpServerRecord>("/mcp-oauth/callback", params, {
    silent: true,
  })
}
