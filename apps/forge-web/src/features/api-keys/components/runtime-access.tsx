import { Link } from "@tanstack/react-router"

import { useScopeAccess } from "@/lib/hierarchy"

/**
 * Who may call an organization's agents and workflows through the runtime:
 * callers with one of its API keys, or signed in, holding agents:run (an
 * agent's or workflow's draft: agents:manage). A key's end users'
 * conversations are its own, apart from every other key's. Links to its API
 * keys for those who manage them.
 */
export function RuntimeAccess({ organizationId }: { organizationId: string }) {
  const can = useScopeAccess(`org:${organizationId}`)
  const header = (
    <code className="font-mono text-[0.9em]">Authorization: Bearer fk_…</code>
  )
  const keys = can("api_keys:manage") ? (
    <Link
      to="/organizations/$organizationId"
      params={{ organizationId }}
      search={{ view: "api-keys" }}
      className="font-medium text-foreground underline-offset-2 hover:underline"
    >
      API keys
    </Link>
  ) : (
    "API keys"
  )
  return (
    <p className="text-xs/[1.6] text-muted-foreground">
      Callers send one of the organization&apos;s {keys} whose role runs agents
      (API caller), as {header}, or a Forge sign-in. Each key&apos;s end
      users&apos; conversations are that key&apos;s alone.
    </p>
  )
}
