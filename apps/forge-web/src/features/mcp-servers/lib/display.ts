import { McpServerIcon } from "@hugeicons/core-free-icons"

import type { IconProp } from "@/components/forge/icons"
import type { McpServerRecord, McpServerStatus } from "./api"

export const MCP_SERVERS_ICON: IconProp = McpServerIcon

type ChipTone = "neutral" | "success" | "warning" | "danger" | "notice"

/** What a server's last check found, as a chip says it. */
export const STATUS_DISPLAY: Record<
  McpServerStatus,
  { label: string; tone: ChipTone }
> = {
  ok: { label: "Connected", tone: "success" },
  needs_auth: { label: "Needs sign-in", tone: "warning" },
  error: { label: "Can't connect", tone: "danger" },
  unchecked: { label: "Not checked", tone: "neutral" },
}

/** A server's status, minding that an OAuth server nobody connected needs it. */
export function statusOf(server: McpServerRecord): McpServerStatus {
  if (server.auth.interactive && server.auth.connected === false)
    return "needs_auth"
  return server.status
}

export const plural = (n: number, word: string) =>
  `${n} ${n === 1 ? word : `${word}s`}`

/** The tools an agent's MCP tool picks, from its comma-separated list. */
export const toolNames = (tools: string) =>
  tools
    .split(",")
    .map((name) => name.trim())
    .filter(Boolean)

/** Where the OAuth callback goes back to, kept across the sign-in's redirects. */
export const OAUTH_RETURN_KEY = "forge:mcp-oauth-return"

export type OAuthReturn = { organizationId: string; serverId: string }
