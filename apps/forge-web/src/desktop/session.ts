import { useQuery } from "@tanstack/react-query"

import type { AgentModel } from "@/features/agents/lib/chat-page"
import { createApiClient } from "@/lib/api/index"

import { DEFAULT_SERVER } from "./links"

/*
 * Who the desktop app is to its Forge server: a person, by a sign-in token
 * sent as `Authorization: Bearer <token>` to the admin API (who they are:
 * `/me`; their organizations' agents: `/me/chat-agents`) and to the
 * runtime, which runs those agents as them, into their own conversations.
 *
 * Single sign-on is to hand the app that token. Until it does, the token is
 * a development one, `VITE_DESKTOP_API_TOKEN`: the desktop's dev server
 * takes the console's own (`VITE_API_TOKEN`, from `make web-token`: the
 * seeded site administrator), and a build only one it's given, so no
 * installer carries a person's token by accident (vite.desktop.config.ts).
 */

/** The sign-in token: the one place single sign-on will set. */
const TOKEN: string | undefined =
  String(import.meta.env.VITE_DESKTOP_API_TOKEN ?? "") || undefined

/** Whether the app has someone signed in. */
export const SIGNED_IN = Boolean(TOKEN)

/** The headers that say who's calling: none when no one is signed in. */
export const signInHeaders = (): Record<string, string> =>
  TOKEN ? { Authorization: `Bearer ${TOKEN}` } : {}

/** The admin API on this app's server, as the person signed in. */
const api = createApiClient({
  baseURL: DEFAULT_SERVER,
  retry: false,
  auth: { getAccessToken: () => TOKEN },
})

/** Who's signed in, as the admin API says (`GET /me`). */
export type Me = {
  /** Their subject: their user ID, and their conversations' `userId`. */
  subject: string
  user: {
    first_name?: string | null
    last_name?: string | null
    email?: string | null
  } | null
}

/** A published agent of one of their organizations (`GET /me/chat-agents`). */
export type MyAgent = {
  id: string
  organization_id: string
  organization_name: string
  name: string
  description: string
  version: number
  agents: Record<string, string>
  workflows: Record<string, string>
  /** The model it starts on (an older server leaves it out). */
  model?: AgentModel
}

/** Who's signed in; nothing is asked when no one is. */
export function useMe() {
  return useQuery({
    queryKey: ["desktop-me", DEFAULT_SERVER],
    queryFn: ({ signal }) => api.get<Me>("/me", { signal, silent: true }),
    enabled: SIGNED_IN && Boolean(DEFAULT_SERVER),
    staleTime: 5 * 60_000,
    meta: { silent: true },
  })
}

/** The published agents of the organizations they're a member of. */
export function useMyAgents(enabled = true) {
  return useQuery({
    queryKey: ["desktop-my-agents", DEFAULT_SERVER],
    queryFn: ({ signal }) =>
      api.get<MyAgent[]>("/me/chat-agents", { signal, silent: true }),
    enabled: enabled && SIGNED_IN && Boolean(DEFAULT_SERVER),
    staleTime: 60_000,
    meta: { silent: true },
  })
}

/** A person's name, as the app shows it: their first and last, or their email. */
export function nameOf(me: Me | undefined): string {
  const first = me?.user?.first_name?.trim() ?? ""
  const last = me?.user?.last_name?.trim() ?? ""
  const name = [first, last].filter(Boolean).join(" ")
  return name || me?.user?.email || me?.subject || ""
}
