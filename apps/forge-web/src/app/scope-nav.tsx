import { useLocation } from "@tanstack/react-router"

import { useAdminNav } from "@/features/admin/components/admin-nav"
import { useShellPage } from "@/components/forge/shell/index"
import { OrganizationWorkspaceNav } from "@/features/organizations/components/organization-workspace-nav"
import { useMyOrganizations } from "@/lib/access"
import { useScopeAccess } from "@/lib/hierarchy"
import { useHasRole } from "@/lib/user-access"
import {
  SITE_ADMIN,
  isAdminPath,
  organizationInPath,
  organizationOf,
  useWorkspaceScope,
} from "@/app/workspace-scope"

/** An organization's workspace nav, with nothing in it open. */
function OrganizationNav({ organizationId }: { organizationId: string }) {
  const myOrganizations = useMyOrganizations()
  // Your membership: only an organization's members get its nav.
  const organization = myOrganizations.data?.find(
    (o) => o.id === organizationId
  )
  const can = useScopeAccess(organization ? `org:${organizationId}` : undefined)

  useShellPage({
    sidebar: {
      label: organization ? `${organization.name} workspace` : "Organization",
    },
  })

  if (!organization) return null
  return (
    <OrganizationWorkspaceNav
      organizationId={organizationId}
      members={can("members:read")}
    />
  )
}

function AdminNav() {
  useAdminNav()
  return null
}

/**
 * The sub nav of where you're working, for pages that don't draw their own
 * (an error, a page not found): the organization whose page failed, else the scope
 * the switcher shows. Site administration's layout keeps its own, so under
 * /admin this adds nothing; an organization's page never falls back to site
 * administration's.
 */
export function ScopeNav() {
  const pathname = useLocation({ select: (location) => location.pathname })
  const scope = useWorkspaceScope((state) => state.scope)
  const isAdmin = useHasRole(SITE_ADMIN)

  if (isAdminPath(pathname)) return null
  const organizationId = organizationInPath(pathname) ?? organizationOf(scope)
  if (organizationId) {
    return (
      <OrganizationNav key={organizationId} organizationId={organizationId} />
    )
  }
  return scope === "admin" && isAdmin ? <AdminNav /> : null
}
