/**
 * The hosted runtime (`{VITE_API_URL}/runtime`): where outside apps call the
 * organizations' agents (ADK's run API, A2A) and workflows.
 */

/** The runtime's base URL: the chat UI's `adk.url` for a chat agent. */
export const RUNTIME_URL: string | undefined = import.meta.env.VITE_API_URL
  ? `${String(import.meta.env.VITE_API_URL).replace(/\/+$/, "")}/runtime`
  : undefined

/**
 * Where the runtime answers `path` (`/run_sse`, `/a2a/ca_x`), as an address
 * to give other apps: absolute when the console knows the API's.
 */
export function runtimeAddress(path: string) {
  return RUNTIME_URL
    ? new URL(`${RUNTIME_URL}${path}`, window.location.origin).toString()
    : `/api/v1/runtime${path}`
}

/** An agent's or workflow's A2A card: `ca_x`, `ag_x@3`, `ca_x@draft`. */
export const a2aCardAddress = (appName: string) =>
  runtimeAddress(`/a2a/${appName}/.well-known/agent-card.json`)
