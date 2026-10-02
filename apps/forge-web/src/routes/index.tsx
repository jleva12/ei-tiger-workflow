import { createFileRoute, Navigate } from "@tanstack/react-router"

import { PageEmpty } from "@/components/forge/empty-state"
import { useShellPage } from "@/components/forge/shell"
import { useMyOrganizations } from "@/lib/access"
import { useHasRole, useUserAccess } from "@/lib/user-access"
import {
  SITE_ADMIN,
  organizationOf,
  useWorkspaceScope,
} from "@/app/workspace-scope"

export const Route = createFileRoute("/")({
  component: HomePage,
})

/**
 * Home opens where you work: the scope you used last if you still can,
 * else site administration for a site administrator, else your first organization.
 * Settings, if open, stays open over it.
 */
function HomePage() {
  const settings = Route.useSearch({ select: (search) => search.settings })
  const scope = useWorkspaceScope((state) => state.scope)
  const accessReady = useUserAccess((state) => state.status === "ready")
  const isAdmin = useHasRole(SITE_ADMIN)
  const organizations = useMyOrganizations()

  useShellPage({ header: { title: "Home", icon: "home" } })

  if (!accessReady || organizations.isPending) return null

  const last = organizations.data?.find(
    (organization) => organization.id === organizationOf(scope)
  )
  if (scope === "admin" && isAdmin)
    return <Navigate to="/admin/organizations" search={{ settings }} replace />
  if (last) {
    return (
      <Navigate
        to="/organizations/$organizationId"
        params={{ organizationId: last.id }}
        search={{ settings }}
        replace
      />
    )
  }
  if (isAdmin)
    return <Navigate to="/admin/organizations" search={{ settings }} replace />
  const first = organizations.data?.[0]
  if (first) {
    return (
      <Navigate
        to="/organizations/$organizationId"
        params={{ organizationId: first.id }}
        search={{ settings }}
        replace
      />
    )
  }
  return (
    <PageEmpty
      illustration="waiting"
      title="You're not in an organization yet"
      description="Ask an organization admin or a site administrator to add you to an organization. It then appears in the switcher at the top of the sidebar."
    />
  )
}
