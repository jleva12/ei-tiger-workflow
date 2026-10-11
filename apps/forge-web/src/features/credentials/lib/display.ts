import { LockKeyIcon } from "@hugeicons/core-free-icons"

import type { IconProp } from "@/components/forge/icons"
import type { CredentialRecord } from "./api"

export const CREDENTIALS_ICON: IconProp = LockKeyIcon

/** A credential as the builders pick it and check what uses it. */
export type CredentialLookup = {
  name: string
  /** Its method's name: Bearer token, OAuth sign-in… */
  method: string
  /** Where it may be sent. */
  urls: string[]
  /** A person signs in with it (OAuth sign-in), and whether someone has. */
  interactive: boolean
  connected: boolean
}

/** A credential's record as its lookup, its method named. */
export const lookupOf = (
  record: CredentialRecord,
  methods: Record<string, string>
): CredentialLookup => ({
  name: record.name,
  method: methods[record.auth.kind] ?? record.auth.kind,
  urls: record.urls,
  interactive: record.auth.interactive,
  connected: !record.auth.interactive || record.auth.connected === true,
})

type Parts = { scheme: string; host: string; port: string; path: string }

function partsOf(url: string): Parts | undefined {
  try {
    const parsed = new URL(url.trim())
    const scheme = parsed.protocol.replace(/:$/, "")
    if (scheme !== "http" && scheme !== "https") return undefined
    return {
      scheme,
      host: parsed.hostname.toLowerCase(),
      port: parsed.port || (scheme === "https" ? "443" : "80"),
      path: parsed.pathname,
    }
  } catch {
    return undefined
  }
}

/**
 * Whether a credential that may be sent to `prefixes` goes with a request to
 * `url`, as the runtime decides (forge_mcp_servers.api_credentials.sends_to):
 * the same scheme, host and port, and the prefix's path or under it.
 */
export function sendsTo(url: string, prefixes: string[]): boolean {
  const target = partsOf(url)
  if (!target) return false
  return prefixes.some((prefix) => {
    const allowed = partsOf(prefix)
    if (
      !allowed ||
      allowed.scheme !== target.scheme ||
      allowed.host !== target.host ||
      allowed.port !== target.port
    )
      return false
    const base = allowed.path.replace(/\/+$/, "")
    const path = target.path || "/"
    return !base || path === base || path.startsWith(`${base}/`)
  })
}

/**
 * Whether a credential goes with a step's or tool's requests, as far as its
 * URL tells before it runs: true or false for a URL written out (or with
 * `{{ }}`, `{argument}` or `${NAME}` only in its query); otherwise false when
 * what's written before the placeholder can't lead under any of the
 * credential's addresses (another host, or another path), and undefined
 * (can't tell) when it can.
 */
export function mayGo(url: string, urls: string[]): boolean | undefined {
  const text = url.trim()
  const cut = text.search(/\{|\$\{/)
  if (cut === -1) return sendsTo(text, urls)
  const written = text.slice(0, cut)
  // Its origin, when the placeholder comes after it.
  const origin = /^https?:\/\/[^/\s?#{}$]+(?=[/?#])/.exec(written)?.[0]
  const target = origin ? partsOf(origin) : undefined
  if (!origin || !target) return undefined
  const rest = written.slice(origin.length)
  const end = rest.search(/[?#]/)
  // Only its query is filled in: where it goes is written out.
  if (end !== -1) return sendsTo(origin + rest.slice(0, end), urls)
  // Its path so far, whole segments resolved as the request's will be; the
  // placeholder goes on from there.
  const whole = rest.slice(0, rest.lastIndexOf("/") + 1)
  const path = new URL(origin + whole).pathname + rest.slice(whole.length)
  const possible = urls.some((u) => {
    const allowed = partsOf(u)
    if (
      !allowed ||
      allowed.scheme !== target.scheme ||
      allowed.host !== target.host ||
      allowed.port !== target.port
    )
      return false
    const base = allowed.path.replace(/\/+$/, "")
    return !base || `${base}/`.startsWith(path) || path.startsWith(`${base}/`)
  })
  return possible ? undefined : false
}

/**
 * Whether a step's or tool's URL is written out (no `{{ }}`, `{argument}` or
 * `${NAME}` in its address part), so where it goes is known before it runs.
 */
export function fixedAddress(url: string): boolean {
  const address = url.trim().split(/[?#]/)[0]
  return /^https?:\/\/[^\s{}$]+$/.test(address)
}

/** Where a credential may go, short: its URLs without the scheme. */
export const sentToLabel = (urls: string[]) =>
  urls
    .map((url) => url.replace(/^https?:\/\//, "").replace(/\/$/, ""))
    .join(", ")

/** A URL's origin and path, the way a credential names where it goes: for a new one's first URL. */
export function originOf(url: string): string | undefined {
  if (!fixedAddress(url)) {
    const origin = /^(https?:\/\/[^/\s{}$?#]+)/.exec(url.trim())?.[1]
    return origin && partsOf(origin) ? origin : undefined
  }
  const parts = partsOf(url)
  if (!parts) return undefined
  return new URL(url.trim()).origin
}

/** Where the OAuth callback goes back to, kept across the sign-in's redirects. */
export const CREDENTIAL_OAUTH_RETURN_KEY = "forge:credential-oauth-return"

export type CredentialOAuthReturn = {
  organizationId: string
  credentialId: string
  /** The page the sign-in started on (a builder), to go back to. */
  path: string
}
