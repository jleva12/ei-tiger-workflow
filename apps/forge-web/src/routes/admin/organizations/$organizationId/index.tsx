import * as React from "react"
import { createFileRoute, Link, useNavigate } from "@tanstack/react-router"

import { LEVELS } from "@/components/admin/levels"
import {
  NodeActions,
  NodeSummary,
  NodeUnavailable,
} from "@/components/admin/node-page"
import { OrganizationMembers } from "@/components/admin/organization-members"
import { PrimaryAction } from "@/components/forge/app-shell"
import { Icon } from "@/components/forge/icon"
import { ShellHeaderActions, useShellPage } from "@/components/forge/shell"
import { Button } from "@/components/ui/button"
import { useMyOrganizations } from "@/lib/access"
import { organizations, useScopeAccess } from "@/lib/hierarchy"
import { usePageContext } from "@/lib/page-context"

/**
 * An organization in site administration: its summary, and who holds which
 * role in it (its own members, and the site's roles that apply there). Its
 * workspace, where its members work, is `/organizations/$organizationId`.
 */
export const Route = createFileRoute("/admin/organizations/$organizationId/")({
  // Site administration: the /admin layout lets only site administrators in.
  component: OrganizationPage,
})

// The dialogs show failures themselves, so skip the error toast.
const SILENT = { meta: { silent: true } }

function OrganizationPage() {
  const { organizationId } = Route.useParams()
  const navigate = useNavigate()
  const removeOrganization = organizations.useDelete(SILENT)
  // Once it's deleted, don't fetch it again (404) before leaving the page.
  const organization = organizations.useDetail(organizationId, {
    enabled: !removeOrganization.isPending && !removeOrganization.isSuccess,
  })
  const can = useScopeAccess(`org:${organizationId}`)
  const updateOrganization = organizations.useUpdate(SILENT)
  const [assigning, setAssigning] = React.useState(false)
  // Only its members open its workspace; a site role alone doesn't make one.
  const member = useMyOrganizations().data?.some((o) => o.id === organizationId)
  const org = organization.data
  const toOrganizations = () => void navigate({ to: "/admin/organizations" })

  useShellPage({
    header: {
      title: org?.name ?? "Organization",
      icon: LEVELS.organization.icon,
      breadcrumbs: [{ label: "Organizations", href: "/admin/organizations" }],
    },
  })
  usePageContext({
    entities: [{ kind: "organization", id: organizationId, label: org?.name }],
  })

  if (organization.error) {
    return (
      <NodeUnavailable
        level={LEVELS.organization}
        error={organization.error}
        onRetry={() => void organization.refetch()}
        back={{ label: "organizations", onClick: toOrganizations }}
      />
    )
  }

  return (
    <>
      <ShellHeaderActions>
        <NodeActions
          level={LEVELS.organization}
          node={org}
          can={can}
          update={(input) =>
            updateOrganization.mutateAsync({ id: organizationId, data: input })
          }
          remove={() => removeOrganization.mutateAsync(organizationId)}
          onDeleted={toOrganizations}
        />
        {member && (
          <Button
            variant="outline"
            nativeButton={false}
            render={
              <Link
                to="/organizations/$organizationId"
                params={{ organizationId }}
              />
            }
          >
            <Icon icon="external" data-icon="inline-start" />
            Open workspace
          </Button>
        )}
        {can("members:update") && (
          <PrimaryAction onClick={() => setAssigning(true)}>
            Member
          </PrimaryAction>
        )}
      </ShellHeaderActions>
      <div className="flex flex-col gap-6">
        <NodeSummary node={org} />
        {org && (
          <OrganizationMembers
            organization={org}
            assigning={assigning}
            onAssigningChange={setAssigning}
          />
        )}
      </div>
    </>
  )
}
