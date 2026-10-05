import { Link } from "@tanstack/react-router"

import { ShellSidebarHeader } from "@/components/forge/shell/index"
import {
  NavItem,
  NavSectionHeading,
  SidebarSection,
} from "@/components/forge/workspace-sidebar"
import {
  DEFAULT_VIEW,
  WORKSPACE_SECTIONS,
  WORKSPACE_VIEWS,
  type WorkspaceView,
} from "@/features/organizations/lib/organization-workspace"

/**
 * An organization workspace's sub nav: Overview on top, then its sections:
 * Workflows and agents (Workflows, Agents), Integrations (MCP servers,
 * Knowledge bases) and, for those who may see its members, Settings
 * (Members). Render it anywhere in the page: it goes in the shell's pinned
 * `ShellSidebarHeader`.
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
  // The default page is the one without a view.
  const viewItem = (key: WorkspaceView) => (
    <NavItem
      key={key}
      icon={WORKSPACE_VIEWS[key].icon}
      active={view === key}
      render={
        <Link
          to="/organizations/$organizationId"
          params={{ organizationId }}
          search={{ view: key === DEFAULT_VIEW ? undefined : key }}
        />
      }
    >
      {WORKSPACE_VIEWS[key].label}
    </NavItem>
  )

  const sections = WORKSPACE_SECTIONS.map((section) => ({
    ...section,
    views: section.views.filter((key) => key !== "config" || members),
  })).filter((section) => section.views.length > 0)

  return (
    <ShellSidebarHeader>
      <SidebarSection variant="primary">{viewItem("overview")}</SidebarSection>
      {sections.map((section, index) => (
        <SidebarSection
          key={section.title}
          // The last one closes the sidebar's top without a rule of its own.
          variant={index === sections.length - 1 ? "flush" : "default"}
        >
          <NavSectionHeading>{section.title}</NavSectionHeading>
          {section.views.map(viewItem)}
        </SidebarSection>
      ))}
    </ShellSidebarHeader>
  )
}
