/*
 * The desktop app's own links: `forge-agents://add?server=…&token=cp_…`.
 * A chat page that offers the app (its `desktop_link` setting) opens one
 * from its "Open in the desktop app" button; the app (apps/forge-desktop)
 * receives it and offers to add the agent. Made and read here, so both ends
 * agree on it.
 *
 * Anything on the web can open such a link, so the app reads it as
 * untrusted: an http(s) server, a page token of the right shape, nothing
 * else (never a visitor ID, which is a visitor's conversations' key).
 */

/** The scheme the desktop app registers (its tauri.conf.json `plugins.deep-link`). */
export const DESKTOP_SCHEME = "forge-agents"

/** A chat page's token, as the admin API makes them (`PAGE_TOKEN_PATTERN`). */
export const PAGE_TOKEN = /^cp_[a-z0-9]{24}$/

/** An agent a link offers: its page's token, and the API it's on. */
export type DesktopAgentLink = { token: string; server: string }

/**
 * A server address as the desktop app keeps it: an absolute http(s) URL of
 * the API (`https://forge.example.com/api/v1`), without a trailing slash;
 * `undefined` for anything else (a relative path, another scheme, a query).
 */
export function serverOf(text: string): string | undefined {
  const trimmed = text.trim()
  if (!trimmed) return undefined
  try {
    const url = new URL(trimmed)
    if (url.protocol !== "https:" && url.protocol !== "http:") return undefined
    if (url.username || url.password || url.search || url.hash) return undefined
    return `${url.origin}${url.pathname}`.replace(/\/+$/, "")
  } catch {
    return undefined
  }
}

/**
 * The API a runtime is on (`{API}/runtime` → `{API}`), as an absolute
 * address: a relative one (the console's `/api/v1`) against `base`.
 */
export function serverOfRuntime(
  runtime: string,
  base: string = window.location.origin
): string | undefined {
  try {
    return serverOf(
      new URL(runtime.replace(/\/runtime\/*$/, ""), base).toString()
    )
  } catch {
    return undefined
  }
}

/** The link that opens a page's agent in the desktop app. */
export function desktopLinkOf({ token, server }: DesktopAgentLink): string {
  const query = new URLSearchParams({ server, token })
  return `${DESKTOP_SCHEME}://add?${query}`
}

/**
 * The agent a desktop app link offers; `undefined` for any other link, or
 * one whose server or token isn't of the right shape.
 */
export function parseDesktopLink(text: string): DesktopAgentLink | undefined {
  let url: URL
  try {
    url = new URL(text.trim())
  } catch {
    return undefined
  }
  if (url.protocol !== `${DESKTOP_SCHEME}:`) return undefined
  // forge-agents://add?…: "add" is the host, or a path where a system
  // hands it over as forge-agents:/add?….
  const action = url.host || url.pathname.replace(/^\/+/, "")
  if (action.toLowerCase() !== "add") return undefined
  const token = url.searchParams.get("token") ?? ""
  const server = serverOf(url.searchParams.get("server") ?? "")
  if (!PAGE_TOKEN.test(token) || !server) return undefined
  return { token, server }
}
