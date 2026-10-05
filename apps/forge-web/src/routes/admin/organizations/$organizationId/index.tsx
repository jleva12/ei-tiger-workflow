import * as React from "react"
import { createFileRoute, Link, useNavigate } from "@tanstack/react-router"

import { LEVELS } from "@/features/admin/components/levels"
import {
  NodeActions,
  NodeSummary,
  NodeUnavailable,
} from "@/features/admin/components/node-page"
import { OrganizationMembers } from "@/features/admin/components/organization-members"
import { PrimaryAction, ViewToolbar } from "@/components/forge/app-shell"
import { Icon } from "@/components/forge/icon"
import {
  ShellHeaderActions,
  ShellToolbar,
  useShellPage,
} from "@/components/forge/shell"
import { ViewTabsList, ViewTabsTrigger } from "@/components/forge/toolbar"
import { Button } from "@/components/ui/button"
import { Tabs, TabsContent } from "@/components/ui/tabs"
import { OrganizationOverview } from "@/features/overview/components/organization-overview"
import { PeriodSwitch } from "@/features/overview/components/panels"
import {
  DEFAULT_PERIOD,
  isOverviewPeriod,
  type OverviewPeriod,
} from "@/features/overview/lib/overview"
import { useMyOrganizations } from "@/lib/access"
import { organizations, useScopeAccess } from "@/lib/hierarchy"
import { usePageContext } from "@/features/assistant/lib/page-context"

type OrganizationSearch = {
  /** Members; the overview when absent. */
  tab?: "members"
  /** The overview's period; 7 days when absent. */
  period?: OverviewPeriod
}

/**
 * An organization in site administration: its overview (what its workflows,
 * agents and assistant did and used, and what it cost: the same as its
 * workspace's, for site administrators who aren't among its members), and
 * its summary and who holds which role in it (its own members, and the
 * site's roles that apply there; `tab=members`). Its workspace, where its
 * members work, is `/organizations/$organizationId`.
 */
export const Route = createFileRoute("/admin/organizations/$organizationId/")({
  validateSearch: (search: Record<string, unknown>): OrganizationSearch => ({
    tab: search.tab === "members" ? "members" : undefined,
    period:
      isOverviewPeriod(search.period) && search.period !== DEFAULT_PERIOD
        ? search.period
        : undefined,
  }),
  // Site administration: the /admin layout lets only site administrators in.
  component: OrganizationPage,
})

// The dialogs show failures themselves, so skip the error toast.
const SILENT = { meta: { silent: true } }

function OrganizationPage() {
  const { organizationId } = Route.useParams()
  const { tab = "overview", period = DEFAULT_PERIOD } = Route.useSearch()
  const setSearch = Route.useNavigate()
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
        {tab === "overview" && (
          <PeriodSwitch
            value={period}
            onValueChange={(next) =>
              void setSearch({
                search: {
                  period: next === DEFAULT_PERIOD ? undefined : next,
                },
                replace: true,
              })
            }
          />
        )}
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
        {tab === "members" && can("members:update") && (
          <PrimaryAction onClick={() => setAssigning(true)}>
            Member
          </PrimaryAction>
        )}
      </ShellHeaderActions>
      <Tabs
        value={tab}
        onValueChange={(value) =>
          void setSearch({
            search: { tab: value === "members" ? "members" : undefined },
          })
        }
        className="gap-0"
      >
        {/* The views, in the toolbar under the top bar; the Tabs around the
            page still pair them with the panels below. */}
        <ShellToolbar>
          <ViewToolbar>
            <ViewTabsList aria-label="Organization views">
              <ViewTabsTrigger value="overview" icon="dashboard">
                Overview
              </ViewTabsTrigger>
              <ViewTabsTrigger value="members" icon="team">
                Members
              </ViewTabsTrigger>
            </ViewTabsList>
          </ViewToolbar>
        </ShellToolbar>
        <TabsContent value="overview">
          {org && (
            <OrganizationOverview
              organizationId={organizationId}
              organizationName={org.name}
              period={period}
              // A run's page is in the workspace, which only members open.
              onOpenRun={
                member
                  ? (runId) =>
                      void navigate({
                        to: "/organizations/$organizationId",
                        params: { organizationId },
                        search: { view: "agents", agentRun: runId },
                      })
                  : undefined
              }
            />
          )}
        </TabsContent>
        <TabsContent value="members">
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
        </TabsContent>
      </Tabs>
    </>
  )
}
