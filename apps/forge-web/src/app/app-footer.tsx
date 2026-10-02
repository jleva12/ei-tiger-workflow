import { useQuery } from "@tanstack/react-query"

import { FooterShortcut, WorkspaceFooter } from "@/components/forge/app-shell"
import { ShellFooter } from "@/components/forge/shell/index"
import { ConnectionDot } from "@/components/forge/status"
import { toApiError } from "@/lib/api/index"
import { api } from "@/lib/api-instance"

// The admin API's probes sit at its root, beside the /api/v1 prefix. Its
// readiness probe also checks the database. VITE_API_URL may be relative
// (/api/v1, through the dev server's proxy): resolve it against the page.
const READY_URL = import.meta.env.VITE_API_URL
  ? new URL(
      "/health/ready",
      new URL(import.meta.env.VITE_API_URL, window.location.origin)
    ).toString()
  : "/health/ready"

// How often to check the API while the app is open.
const CHECK_EVERY = 30_000

type ServiceInfo = { name: string; version: string }

/** Whether the admin API, and its database, answer; and its version. */
function useApiStatus() {
  const ready = useQuery({
    queryKey: ["admin-api", "ready"],
    queryFn: ({ signal }) => api.get<{ status: string }>(READY_URL, { signal }),
    refetchInterval: CHECK_EVERY,
    // The footer shows a failure; don't retry it or toast it.
    retry: false,
    meta: { silent: true },
  })
  const info = useQuery({
    queryKey: ["admin-api", "info"],
    queryFn: ({ signal }) => api.get<ServiceInfo>("/info", { signal }),
    staleTime: Infinity,
    meta: { silent: true },
  })
  if (ready.isPending) {
    return { online: false, label: "Checking the admin API…" }
  }
  if (ready.error) {
    // 503: the API answers, but its database doesn't.
    const unready = toApiError(ready.error).status === 503
    return {
      online: false,
      label: unready
        ? "Admin API up, database unavailable"
        : "Admin API unreachable",
    }
  }
  return {
    online: true,
    label: `Admin API connected${info.data ? ` · v${info.data.version}` : ""}`,
  }
}

/**
 * The status bar under every page: whether the admin API is up, and a
 * keyboard hint. A page can replace it with its own `ShellFooter`.
 */
export function AppFooter() {
  const { online, label } = useApiStatus()
  return (
    <ShellFooter>
      <WorkspaceFooter>
        <span role="status">
          <ConnectionDot online={online} />
          {label}
        </span>
        <FooterShortcut keys="D">Theme</FooterShortcut>
      </WorkspaceFooter>
    </ShellFooter>
  )
}
