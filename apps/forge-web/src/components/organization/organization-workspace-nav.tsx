import { Link } from "@tanstack/react-router"

import { AssistantMark } from "@/components/forge/assistant"
import { ShellSidebarHeader } from "@/components/forge/shell"
import { NavItem, SidebarSection } from "@/components/forge/workspace-sidebar"
import {
  WORKSPACE_VIEWS,
  type WorkspaceView,
} from "@/lib/organization-workspace"

/**
 * An organization workspace's sub nav: Workflows, ADK workflows, Agents
 * (wearing the assistant's mark), Events and, for those who may see its
 * members, Members. Render it anywhere in the page: it goes in
 * the shell's pinned `ShellSidebarHeader`.
 */
export function OrganizationWorkspaceNav({
  organizationId,
  view,
  members,
}: {
  organizationId: string
  /** The page open; none when it isn't one of them (an error page). */
  view?: WorkspaceView
  /** You may see its members (`members:read`): show Members. */
  members: boolean
}) {
  // Workflows is the page without a view.
  const viewItem = (key: WorkspaceView) => (
    <NavItem
      key={key}
      icon={WORKSPACE_VIEWS[key].icon}
      mark={
        "mark" in WORKSPACE_VIEWS[key] ? (
          <AssistantMark className="size-[19px]" />
        ) : undefined
      }
      active={view === key}
      render={
        <Link
          to="/organizations/$organizationId"
          params={{ organizationId }}
          search={{ view: key === "workflows" ? undefined : key }}
        />
      }
    >
      {WORKSPACE_VIEWS[key].label}
    </NavItem>
  )

  return (
    <ShellSidebarHeader>
      <SidebarSection variant="primary">
        {viewItem("workflows")}
        {viewItem("agents")}
        {viewItem("chat-agents")}
        {viewItem("events")}
        {members && viewItem("config")}
      </SidebarSection>
    </ShellSidebarHeader>
  )
}
