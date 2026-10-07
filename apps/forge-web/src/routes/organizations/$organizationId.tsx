import * as React from "react"
import { createFileRoute } from "@tanstack/react-router"

import { LEVELS } from "@/features/admin/components/levels"
import { AdkRunPage } from "@/features/runs/components/adk-run-page"
import { OrganizationOverview } from "@/features/overview/components/organization-overview"
import { PeriodSwitch } from "@/features/overview/components/panels"
import {
  DEFAULT_PERIOD,
  isOverviewPeriod,
} from "@/features/overview/lib/overview"
import { OrganizationAgents } from "@/features/adk-workflows/components/organization-agents"
import { OrganizationChatAgents } from "@/features/agents/components/organization-chat-agents"
import { OrganizationMcpServers } from "@/features/mcp-servers/components/organization-mcp-servers"
import { OrganizationCodeRepositories } from "@/features/code-repositories/components/organization-code-repositories"
import { OrganizationApiKeys } from "@/features/api-keys/components/organization-api-keys"
import { OrganizationKnowledgeBases } from "@/features/knowledge/components/organization-knowledge-bases"
import { OrganizationMembers } from "@/features/admin/components/organization-members"
import { PrimaryAction } from "@/components/forge/app-shell"
import { PageEmpty } from "@/components/forge/empty-state"
import { ErrorCallout } from "@/components/forge/feedback"
import { ShellHeaderActions, useShellPage } from "@/components/forge/shell"
import { OrganizationWorkspaceNav } from "@/features/organizations/components/organization-workspace-nav"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import { useMyOrganizations } from "@/lib/access"
import { isAdkRunTab } from "@/features/runs/lib/runs"
import { useScopeAccess } from "@/lib/hierarchy"
import { usePageContext } from "@/features/assistant/lib/page-context"
import {
  DEFAULT_VIEW,
  isWorkspaceView,
  WORKSPACE_VIEWS,
  type WorkspaceSearch,
} from "@/features/organizations/lib/organization-workspace"

/**
 * An organization's workspace, where its members work: what the switcher
 * opens when you pick one of your organizations, with its own sub nav. Only
 * its members (anyone holding a role in the organization) get in; site roles,
 * site administration included, don't. Overview, the default, is what its
 * workflows, agents and assistant did and used over 7, 30 or 90 days
 * (`period`), each run waiting on people opening its page; Workflows lists
 * its Google ADK graph workflows, each opening its builder at
 * /organizations/$organizationId/agents/$agentId, and their runs by status
 * (`agentsTab=runs`), each opening its own page (`agentRun`, with
 * `agentRunTab`); Agents lists its chat agents, each opening its builder at
 * /organizations/$organizationId/chat-agents/$chatAgentId; MCP servers lists
 * the remote MCP servers its agents use as tools, one open in a dialog
 * (`mcpServer`); Code repositories lists the GitHub repositories it ingests
 * into the code graph, one open in a side panel (`repository`); Members is who
 * holds which role in it, which its admins change. Configuring an
 * organization is also site administration, at
 * /admin/organizations/$organizationId.
 */
export const Route = createFileRoute("/organizations/$organizationId")({
  validateSearch: (search: Record<string, unknown>): WorkspaceSearch => {
    const view = isWorkspaceView(search.view) ? search.view : DEFAULT_VIEW
    return {
      view: view === DEFAULT_VIEW ? undefined : view,
      ...(view === "overview" &&
        isOverviewPeriod(search.period) &&
        search.period !== DEFAULT_PERIOD && { period: search.period }),
      ...(view === "agents" &&
        search.agentsTab === "runs" && { agentsTab: "runs" as const }),
      ...(view === "agents" &&
        typeof search.agentRun === "string" &&
        search.agentRun && {
          agentRun: search.agentRun,
          agentRunTab:
            isAdkRunTab(search.agentRunTab) && search.agentRunTab !== "overview"
              ? search.agentRunTab
              : undefined,
        }),
      ...(view === "mcp-servers" &&
        typeof search.mcpServer === "string" &&
        search.mcpServer && { mcpServer: search.mcpServer }),
      ...(view === "code-repositories" &&
        typeof search.repository === "string" &&
        search.repository && { repository: search.repository }),
    }
  },
  component: OrganizationWorkspacePage,
})

function OrganizationWorkspacePage() {
  const { organizationId } = Route.useParams()
  const {
    view = DEFAULT_VIEW,
    period = DEFAULT_PERIOD,
    agentsTab = "overview",
    agentRun,
    agentRunTab = "overview",
    mcpServer,
    repository,
  } = Route.useSearch()
  const navigate = Route.useNavigate()
  const myOrganizations = useMyOrganizations()
  // Your membership: the organization as your organizations list it, or nothing.
  const organization = myOrganizations.data?.find(
    (o) => o.id === organizationId
  )
  const can = useScopeAccess(organization ? `org:${organizationId}` : undefined)
  // Those who manage its runs (its admins) retry, resubmit and abandon them.
  const canManageRuns = can("agents:manage_runs")
  const canReadMembers = can("members:read")
  const canManageKeys = can("api_keys:manage")
  const [assigning, setAssigning] = React.useState(false)

  useShellPage({
    header: organization
      ? {
          title: WORKSPACE_VIEWS[view].label,
          icon: WORKSPACE_VIEWS[view].icon,
          breadcrumbs: [
            {
              label: organization.name,
              href: `/organizations/${organizationId}`,
            },
          ],
        }
      : {
          title: "Organization",
          icon: LEVELS.organization.icon,
          breadcrumbs: [],
        },
    // The workspace draws its own sub nav (below), not site administration's.
    sidebar: {
      label: organization ? `${organization.name} workspace` : "Organization",
      sections: [],
    },
  })
  // For the assistant: the organization. The view and what's open in it are
  // in the URL, which it gets too.
  usePageContext({
    entities: [
      { kind: "organization", id: organizationId, label: organization?.name },
    ],
  })

  if (myOrganizations.isPending) {
    return (
      <div className="flex flex-col gap-3" aria-busy="true">
        <Skeleton className="h-5 w-48" />
        <Skeleton className="h-4 w-80 max-w-full" />
      </div>
    )
  }
  if (myOrganizations.error) {
    return (
      <ErrorCallout
        title="Couldn't load your organizations"
        action={
          <Button
            variant="outline"
            size="sm"
            onClick={() => void myOrganizations.refetch()}
          >
            Retry
          </Button>
        }
      >
        {myOrganizations.error.message}
      </ErrorCallout>
    )
  }
  if (!organization) {
    return (
      <PageEmpty
        illustration="locked"
        title="You're not in this organization"
        description="Only an organization's members can open its workspace. Pick one of your organizations at the top of the sidebar, or ask one of the organization's admins to add you."
      />
    )
  }

  return (
    <>
      <OrganizationWorkspaceNav
        organizationId={organizationId}
        view={view}
        members={canReadMembers}
        apiKeys={canManageKeys}
      />
      {view === "overview" ? (
        <>
          <ShellHeaderActions>
            <PeriodSwitch
              value={period}
              onValueChange={(next) =>
                void navigate({
                  search: {
                    period: next === DEFAULT_PERIOD ? undefined : next,
                  },
                  replace: true,
                })
              }
            />
          </ShellHeaderActions>
          <OrganizationOverview
            organizationId={organizationId}
            organizationName={organization.name}
            period={period}
            onOpenRun={(id) =>
              void navigate({ search: { view: "agents", agentRun: id } })
            }
          />
        </>
      ) : view === "config" ? (
        !canReadMembers ? (
          <PageEmpty
            illustration="locked"
            title="You can't see this organization's members"
            description={`Ask one of ${organization.name}'s admins who holds which role.`}
          />
        ) : (
          <>
            {can("members:update") && (
              <ShellHeaderActions>
                <PrimaryAction onClick={() => setAssigning(true)}>
                  Member
                </PrimaryAction>
              </ShellHeaderActions>
            )}
            <OrganizationMembers
              organization={organization}
              assigning={assigning}
              onAssigningChange={setAssigning}
            />
          </>
        )
      ) : view === "api-keys" ? (
        !canManageKeys ? (
          <PageEmpty
            illustration="locked"
            title="You can't manage this organization's API keys"
            description={`Ask one of ${organization.name}'s admins for a key, or for the API keys permission.`}
          />
        ) : (
          <OrganizationApiKeys
            organizationId={organizationId}
            organizationName={organization.name}
          />
        )
      ) : view === "mcp-servers" ? (
        <OrganizationMcpServers
          organizationId={organizationId}
          organizationName={organization.name}
          openServer={mcpServer}
          onOpenServer={(id) =>
            void navigate({
              search: (prev) => ({ ...prev, mcpServer: id }),
              replace: true,
            })
          }
        />
      ) : view === "code-repositories" ? (
        <OrganizationCodeRepositories
          organizationId={organizationId}
          organizationName={organization.name}
          openRepository={repository}
          onOpenRepository={(id) =>
            void navigate({
              search: (prev) => ({ ...prev, repository: id }),
              replace: true,
            })
          }
        />
      ) : view === "knowledge" ? (
        // Each knowledge base opens its own page.
        <OrganizationKnowledgeBases
          organizationId={organizationId}
          organizationName={organization.name}
        />
      ) : view === "chat-agents" ? (
        // Each chat agent opens its builder, a page of its own.
        <OrganizationChatAgents
          organizationId={organizationId}
          organizationName={organization.name}
        />
      ) : agentRun ? (
        // Each run opens its task page, with its steps and what it waits for.
        <AdkRunPage
          key={agentRun}
          organization={organization}
          runId={agentRun}
          tab={agentRunTab}
          canManage={canManageRuns}
          onTabChange={(next) =>
            void navigate({
              search: (prev) => ({
                ...prev,
                agentRunTab: next === "overview" ? undefined : next,
              }),
              replace: true,
            })
          }
          onBack={() =>
            void navigate({ search: { view: "agents", agentsTab: "runs" } })
          }
          onOpenRun={(id) =>
            void navigate({ search: { view: "agents", agentRun: id } })
          }
        />
      ) : (
        // Each workflow opens its builder, a page of its own.
        <OrganizationAgents
          organizationId={organizationId}
          organizationName={organization.name}
          tab={agentsTab}
          onTabChange={(next) =>
            void navigate({
              search: {
                view: "agents",
                agentsTab: next === "runs" ? "runs" : undefined,
              },
              replace: true,
            })
          }
          onOpenRun={(id) =>
            void navigate({ search: { view: "agents", agentRun: id } })
          }
        />
      )}
    </>
  )
}
