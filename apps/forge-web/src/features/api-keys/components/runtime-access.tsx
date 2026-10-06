import { Link } from "@tanstack/react-router"

import { useScopeAccess } from "@/lib/hierarchy"
import { useServiceInfo } from "@/lib/service-info"

/**
 * Who may call an organization's agents and workflows through the runtime,
 * as the deployment has it: anyone (a key then says whose the calls are),
 * or callers with one of its API keys, or signed in, holding agents:run.
 * Links to its API keys for those who manage them.
 */
export function RuntimeAccess({ organizationId }: { organizationId: string }) {
  const info = useServiceInfo()
  const can = useScopeAccess(`org:${organizationId}`)
  if (!info.data) return null
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
  return info.data.agent_runtime_public ? (
    <p className="text-xs/[1.6] text-muted-foreground">
      Anyone with its address can call it: this deployment&apos;s runtime is
      public. Send one of the organization&apos;s {keys} ({header}) and the
      calls are that key&apos;s.
    </p>
  ) : (
    <p className="text-xs/[1.6] text-muted-foreground">
      Callers send one of the organization&apos;s {keys} whose role runs agents
      (API caller), as {header}, or a Forge sign-in.
    </p>
  )
}
