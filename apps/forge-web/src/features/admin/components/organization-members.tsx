import * as React from "react"

import { useScopeAccess, type Organization, type Scope } from "@/lib/hierarchy"
import type { AssignTarget } from "./assign-role-dialog"
import { ScopeMembers } from "./scope-members"

/**
 * Who holds which role in an organization: its own members, and the site's
 * roles that apply there. Site administration shows it on the
 * organization's page, and the organization's workspace under Members.
 * Assigning and revoking takes `members:update` in the organization, its
 * admins'; the page's primary action opens the assign dialog.
 */
export function OrganizationMembers({
  organization,
  assigning,
  onAssigningChange,
}: {
  organization: Pick<Organization, "id" | "name">
  assigning: boolean
  onAssigningChange: (open: boolean) => void
}) {
  const scope: Scope = `org:${organization.id}`
  const can = useScopeAccess(scope)
  const scopeLabel = React.useCallback(
    (where: Scope) => (where === "site" ? "Site" : organization.name),
    [organization.name]
  )
  const targets = React.useMemo<AssignTarget[]>(
    () => [{ scope, label: organization.name, level: "org" }],
    [scope, organization.name]
  )
  return (
    <ScopeMembers
      scope={scope}
      scopeLabel={scopeLabel}
      targets={targets}
      canManage={can("members:update")}
      assigning={assigning}
      onAssigningChange={onAssigningChange}
      description="Everyone with a role here: the organization's members, and the site's roles that apply in it."
    />
  )
}
