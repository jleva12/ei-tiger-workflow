import * as React from "react"
import { createFileRoute } from "@tanstack/react-router"

import { LEVELS } from "@/components/admin/levels"
import { AdkRunPage } from "@/components/agents/adk-run-page"
import { OrganizationAgents } from "@/components/agents/organization-agents"
import { OrganizationChatAgents } from "@/components/chat-agents/organization-chat-agents"
import { OrganizationMembers } from "@/components/admin/organization-members"
import {
  BackgroundTaskDetail,
  type TaskPageSection,
} from "@/components/background-tasks/background-task-detail"
import { EventTypePage } from "@/components/events/event-type-page"
import { OrganizationEvents } from "@/components/events/organization-events"
import { PrimaryAction } from "@/components/forge/app-shell"
import { PageEmpty } from "@/components/forge/empty-state"
import { ErrorCallout } from "@/components/forge/feedback"
import { ShellHeaderActions, useShellPage } from "@/components/forge/shell"
import { OrganizationWorkspaceNav } from "@/components/organization/organization-workspace-nav"
import { OrganizationWorkflows } from "@/components/workflows/organization-workflows"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import { useMyOrganizations } from "@/lib/access"
import { isAdkRunTab } from "@/lib/agents/runs"
import { isBackgroundTaskTab } from "@/lib/background-tasks"
import { isEventsTab, isEventTypeTab } from "@/lib/events"
import { useScopeAccess } from "@/lib/hierarchy"
import { usePageContext } from "@/lib/page-context"
import {
  isWorkspaceView,
  WORKSPACE_VIEWS,
  type WorkspaceSearch,
} from "@/lib/organization-workspace"

// A workflow run's page, under Workflows.
const WORKFLOW_TASKS: TaskPageSection = {
  label: WORKSPACE_VIEWS.workflows.label,
  icon: WORKSPACE_VIEWS.workflows.icon,
  back: "All workflow tasks",
  missing: "Workflow run not found",
}

/**
 * An organization's workspace, where its members work: what the switcher
 * opens when you pick one of your organizations, with its own sub nav. Only
 * its members (anyone holding a role in the organization) get in; site roles,
 * site administration included, don't. Workflows, the default, lists its
 * workflows (Overview), each opening its builder at
 * /organizations/$organizationId/workflows/$workflowId, and their runs by
 * status (`workflowsTab=tasks`), each opening its own page (`workflowTask`,
 * with `workflowTaskTab`); ADK workflows lists its Google ADK graph
 * workflows, each opening its builder at
 * /organizations/$organizationId/agents/$agentId, and their runs by status
 * (`agentsTab=runs`), each opening its own page (`agentRun`, with
 * `agentRunTab`); Agents lists its chat
 * agents, each opening its builder at
 * /organizations/$organizationId/chat-agents/$chatAgentId; Events is
 * the organization's inbound endpoint and
 * the event types it accepts, each opening its own page (`eventType`, with
 * `eventTab`) where its schema is built; Members is who holds which role in
 * it, which its admins change. Configuring an organization is also site
 * administration, at /admin/organizations/$organizationId.
 */
export const Route = createFileRoute("/organizations/$organizationId")({
  validateSearch: (search: Record<string, unknown>): WorkspaceSearch => {
    const view = isWorkspaceView(search.view) ? search.view : "workflows"
    return {
      view: view === "workflows" ? undefined : view,
      ...(view === "events" &&
        isEventsTab(search.eventsTab) &&
        search.eventsTab !== "types" && { eventsTab: search.eventsTab }),
      ...(view === "events" &&
        typeof search.eventType === "string" &&
        search.eventType && {
          eventType: search.eventType,
          eventTab:
            isEventTypeTab(search.eventTab) && search.eventTab !== "definition"
              ? search.eventTab
              : undefined,
        }),
      ...(view === "workflows" &&
        search.workflowsTab === "tasks" && { workflowsTab: "tasks" as const }),
      ...(view === "workflows" &&
        typeof search.workflowTask === "string" &&
        search.workflowTask && {
          workflowTask: search.workflowTask,
          workflowTaskTab:
            isBackgroundTaskTab(search.workflowTaskTab) &&
            search.workflowTaskTab !== "overview"
              ? search.workflowTaskTab
              : undefined,
        }),
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
    }
  },
  component: OrganizationWorkspacePage,
})

function OrganizationWorkspacePage() {
  const { organizationId } = Route.useParams()
  const {
    view = "workflows",
    eventsTab = "types",
    eventType,
    eventTab = "definition",
    workflowsTab = "overview",
    workflowTask,
    workflowTaskTab = "overview",
    agentsTab = "overview",
    agentRun,
    agentRunTab = "overview",
  } = Route.useSearch()
  const navigate = Route.useNavigate()
  const myOrganizations = useMyOrganizations()
  // Your membership: the organization as your organizations list it, or nothing.
  const organization = myOrganizations.data?.find(
    (o) => o.id === organizationId
  )
  const can = useScopeAccess(organization ? `org:${organizationId}` : undefined)
  // Those who manage its background tasks (its admins) retry, resubmit and
  // abandon its workflow runs and ADK workflow runs.
  const canManageBackgroundTasks = can("background_tasks:manage")
  const canReadMembers = can("members:read")
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
      />
      {view === "config" ? (
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
      ) : view === "agents" ? (
        agentRun ? (
          // Each run opens its task page, with its steps and what it waits for.
          <AdkRunPage
            key={agentRun}
            organization={organization}
            taskId={agentRun}
            tab={agentRunTab}
            canManage={canManageBackgroundTasks}
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
          />
        ) : (
          // Each ADK workflow opens its builder, a page of its own.
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
        )
      ) : view === "chat-agents" ? (
        // Each chat agent opens its builder, a page of its own.
        <OrganizationChatAgents organizationId={organizationId} organizationName={organization.name} />
      ) : view === "events" ? (
        eventType ? (
          <EventTypePage
            organization={organization}
            eventTypeId={eventType}
            tab={eventTab}
            onTabChange={(next) =>
              void navigate({
                search: (prev) => ({
                  ...prev,
                  eventTab: next === "definition" ? undefined : next,
                }),
                replace: true,
              })
            }
            onBack={() => void navigate({ search: { view: "events" } })}
            onCreated={(id) =>
              void navigate({
                search: { view: "events", eventType: id },
                replace: true,
              })
            }
          />
        ) : (
          <OrganizationEvents
            organizationId={organizationId}
            organizationName={organization.name}
            tab={eventsTab}
            onTabChange={(next) =>
              void navigate({
                search: {
                  view: "events",
                  eventsTab: next === "types" ? undefined : next,
                },
                replace: true,
              })
            }
            onOpenEventType={(id) =>
              void navigate({ search: { view: "events", eventType: id } })
            }
          />
        )
      ) : workflowTask ? (
        // Each run opens its task page, where those who manage background
        // tasks act on it.
        <BackgroundTaskDetail
          key={workflowTask}
          organization={organization}
          taskId={workflowTask}
          tab={workflowTaskTab}
          canManage={canManageBackgroundTasks}
          section={WORKFLOW_TASKS}
          onTabChange={(next) =>
            void navigate({
              search: (prev) => ({
                ...prev,
                workflowTaskTab: next === "overview" ? undefined : next,
              }),
              replace: true,
            })
          }
          onBack={() => void navigate({ search: { workflowsTab: "tasks" } })}
        />
      ) : (
        // Each workflow opens its builder, a page of its own.
        <OrganizationWorkflows
          organizationId={organizationId}
          organizationName={organization.name}
          tab={workflowsTab}
          onTabChange={(next) =>
            void navigate({
              search: { workflowsTab: next === "tasks" ? "tasks" : undefined },
              replace: true,
            })
          }
          onOpenRun={(id) => void navigate({ search: { workflowTask: id } })}
        />
      )}
    </>
  )
}
