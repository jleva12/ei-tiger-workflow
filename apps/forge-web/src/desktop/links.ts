import { parseDesktopLink, serverOf } from "@/features/agents/lib/desktop-link"

/*
 * What a person pastes to add an agent to the desktop app: a chat page's
 * link (`https://forge.example.com/chat/cp_…`), its runtime address
 * (`…/api/v1/runtime/pages/cp_…`), the app's own link
 * (`forge-agents://add?server=…&token=cp_…`) or the bare token (`cp_…`),
 * and the server (the API) its page is on.
 *
 * A page's console link names the console, not the API (in production the
 * console's web server doesn't forward /api), so the server comes from the
 * link only when the link is the runtime's or the app's own; otherwise from
 * the server field, which starts as this build's (`VITE_DESKTOP_API_URL`).
 */

export { serverOf }

/** A chat page's token, anywhere in what was pasted (`PAGE_TOKEN_PATTERN`). */
const TOKEN = /\bcp_[a-z0-9]{24}\b/

/** The server new agents are on unless the person names another: this build's. */
export const DEFAULT_SERVER: string =
  serverOf(String(import.meta.env.VITE_DESKTOP_API_URL ?? "")) ??
  serverOf(String(import.meta.env.VITE_API_URL ?? "")) ??
  ""

/** The runtime on a server: where its chat pages answer. */
export const runtimeOf = (server: string) => `${server}/runtime`

/**
 * The page a pasted link names: its token, and its server when the link is
 * the runtime's own (`{server}/runtime/pages/cp_…`) or the app's
 * (`forge-agents://add?…`); `undefined` when it holds no token.
 */
export function parseChatLink(
  text: string
): { token: string; server?: string } | undefined {
  const offered = parseDesktopLink(text)
  if (offered) return offered
  const token = TOKEN.exec(text)?.[0]
  if (!token) return undefined
  try {
    const url = new URL(text.trim())
    const at = url.pathname.indexOf(`/runtime/pages/${token}`)
    if (at >= 0) {
      const server = serverOf(`${url.origin}${url.pathname.slice(0, at)}`)
      if (server) return { token, server }
    }
  } catch {
    // Not a URL: the bare token, or a console link with words around it.
  }
  return { token }
}
