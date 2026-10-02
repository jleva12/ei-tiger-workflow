import * as React from "react"

import { Button } from "@/components/ui/button"
import { Tabs } from "@/components/ui/tabs"
import {
  FooterShortcut,
  PrimaryAction,
  ViewToolbar,
  WorkspaceFooter,
} from "@/components/forge/app-shell"
import {
  Stat,
  StatGrid,
  ViewHeading,
  WorkspaceView,
} from "@/components/forge/activity"
import { WorkspaceOrb } from "@/components/forge/avatars"
import { PageEmpty } from "@/components/forge/empty-state"
import { Icon } from "@/components/forge/icon"
import {
  PreferenceOptions,
  PreferencesPanel,
} from "@/components/forge/preferences"
import { SettingsList, SettingsRow } from "@/components/forge/settings-list"
import {
  ShellFooter,
  ShellHeaderActions,
  ShellLayout,
  ShellProvider,
  ShellSidebarTop,
  ShellToolbar,
  useShellPage,
  type ShellConfig,
} from "@/components/forge/shell"
import { ViewTabsList, ViewTabsTrigger } from "@/components/forge/toolbar"
import {
  CommandButton,
  SidebarBrandName,
  SidebarCollapseButton,
  SidebarSection,
} from "@/components/forge/workspace-sidebar"
import { fromCasbin, type UserAccess } from "@/lib/user-access"
import { Guard, UserAccessProvider } from "@/lib/user-access-provider"

/* The rail every page shares; pages add the sub nav and header. */
const initial: ShellConfig = {
  rail: {
    items: [
      { id: "home", label: "Home", icon: "home", href: "/home" },
      { id: "tasks", label: "Tasks", icon: "task", href: "/tasks" },
      {
        id: "agents",
        label: "Agents",
        icon: "robot",
        href: "/agents",
        permission: ["agents", "read"],
      },
    ],
    footer: [
      {
        id: "settings",
        label: "Settings",
        icon: "settings",
        href: "/settings",
      },
    ],
  },
}

type Navigate = (href: string) => void

function HomePage({ navigate }: { navigate: Navigate }) {
  useShellPage({
    header: { title: "Home", icon: "home" },
    // A full-width page: no sub nav (the mobile menu still has the rail).
    sidebar: { hidden: true },
  })
  return (
    <WorkspaceView>
      <ViewHeading
        title="Good morning"
        icon="home"
        description="The rail stays; this page hides the sub nav. Open Tasks, Agents or Settings to see each area bring its own."
      />
      <StatGrid>
        <Stat label="Open tasks" value="12" />
        <Stat label="In review" value="2" />
        <Stat label="Agents running" value="3" />
      </StatGrid>
      <ShellHeaderActions>
        <PrimaryAction onClick={() => navigate("/tasks")}>Task</PrimaryAction>
      </ShellHeaderActions>
    </WorkspaceView>
  )
}

const taskViews = {
  "/tasks": { title: "All tasks", illustration: "tasks" },
  "/tasks/mine": { title: "Assigned to me", illustration: "complete" },
  "/tasks/review": { title: "Needs review", illustration: "waiting" },
  "/tasks/project/web": { title: "workspace/web", illustration: "search" },
  "/tasks/project/admin-api": {
    title: "workspace/admin-api",
    illustration: "search",
  },
} as const

function TasksPage({ path }: { path: string }) {
  const [inReview, setInReview] = React.useState(2)
  const [layout, setLayout] = React.useState("list")
  const view = taskViews[path as keyof typeof taskViews] ?? taskViews["/tasks"]

  // One layout for every /tasks page: its sub nav stays mounted while the
  // title follows the path, and the review count follows this page's state.
  useShellPage({
    sidebar: {
      label: "Tasks",
      sections: [
        {
          id: "views",
          title: "Views",
          items: [
            { id: "all", label: "All tasks", icon: "list", href: "/tasks" },
            {
              id: "mine",
              label: "Assigned to me",
              icon: "team",
              href: "/tasks/mine",
              meta: 4,
            },
            {
              id: "review",
              label: "Needs review",
              icon: "review",
              href: "/tasks/review",
              meta: inReview,
            },
          ],
        },
        {
          id: "projects",
          title: "Projects",
          items: [
            {
              id: "web",
              label: "web",
              icon: "folder",
              href: "/tasks/project/web",
            },
            {
              id: "admin-api",
              label: "admin-api",
              icon: "folder",
              href: "/tasks/project/admin-api",
            },
          ],
        },
      ],
    },
    header: {
      breadcrumbs: [{ label: "Tasks", icon: "task", href: "/tasks" }],
      title: view.title,
    },
  })

  return (
    <>
      <PageEmpty
        title={view.title}
        illustration={view.illustration}
        description="The sub nav, title, toolbar, actions and footer all come from this page."
      />
      <ShellHeaderActions>
        <Button
          variant="outline"
          size="sm"
          onClick={() => setInReview((n) => n + 1)}
        >
          Request review
        </Button>
        <Guard permission={["tasks", "create"]}>
          <PrimaryAction>Task</PrimaryAction>
        </Guard>
      </ShellHeaderActions>
      <ShellToolbar>
        <ViewToolbar>
          <Tabs
            value={layout}
            onValueChange={(value) => setLayout(String(value))}
          >
            <ViewTabsList aria-label="Layout">
              <ViewTabsTrigger value="list" icon="list">
                List
              </ViewTabsTrigger>
              <ViewTabsTrigger value="board" icon="kanban">
                Board
              </ViewTabsTrigger>
            </ViewTabsList>
          </Tabs>
        </ViewToolbar>
      </ShellToolbar>
      <ShellFooter>
        <WorkspaceFooter>
          <span>{inReview} waiting for review</span>
          <FooterShortcut keys="C">New task</FooterShortcut>
        </WorkspaceFooter>
      </ShellFooter>
    </>
  )
}

function AgentsPage() {
  useShellPage({
    sidebar: {
      label: "Agents",
      sections: [
        {
          id: "agents",
          title: "Agents",
          items: [
            {
              id: "coder",
              label: "Coder",
              icon: "code",
              href: "/agents",
              meta: "running",
            },
            {
              id: "reviewer",
              label: "Reviewer",
              icon: "review",
              href: "/agents/reviewer",
            },
            {
              id: "planner",
              label: "Planner",
              icon: "sparkles",
              href: "/agents/planner",
            },
          ],
        },
      ],
    },
    header: { title: "Agents", icon: "robot" },
  })
  return (
    <PageEmpty
      title="Agents"
      icon="robot"
      description="Only people with agents · read see this area in the rail."
    />
  )
}

function SettingsPage({ path }: { path: string }) {
  useShellPage({
    sidebar: {
      label: "Settings",
      sections: [
        {
          id: "account",
          title: "Account",
          items: [
            {
              id: "profile",
              label: "Profile",
              icon: "team",
              href: "/settings",
            },
            {
              id: "display",
              label: "Display",
              icon: "view",
              href: "/settings/display",
            },
          ],
        },
        {
          id: "workspace",
          title: "Workspace",
          items: [
            {
              id: "members",
              label: "Members",
              icon: "team",
              href: "/settings/members",
              role: "admin",
            },
            {
              id: "billing",
              label: "Billing",
              icon: "coins",
              href: "/settings/billing",
              role: "admin",
            },
          ],
        },
      ],
    },
    header: {
      breadcrumbs: [{ label: "Settings", icon: "settings", href: "/settings" }],
      title: path === "/settings/display" ? "Display" : "Profile",
    },
  })
  return (
    <WorkspaceView>
      {path === "/settings/display" ? (
        <>
          <ViewHeading title="Display" icon="view" />
          <PreferencesPanel showTheme={false} />
        </>
      ) : (
        <>
          <ViewHeading
            title="Profile"
            icon="team"
            description="Members and Billing only appear in the sub nav for admins."
          />
          <SettingsList>
            <SettingsRow title="Name" meta="Joseph" />
            <SettingsRow title="Email" meta="joseph@acme.dev" />
          </SettingsList>
        </>
      )}
    </WorkspaceView>
  )
}

function Page({ path, navigate }: { path: string; navigate: Navigate }) {
  if (path.startsWith("/tasks")) return <TasksPage path={path} />
  if (path.startsWith("/agents")) return <AgentsPage />
  if (path.startsWith("/settings")) return <SettingsPage path={path} />
  return <HomePage navigate={navigate} />
}

const access: Record<string, UserAccess> = {
  admin: fromCasbin({ roles: ["admin"], policies: [["admin", "*", "*"]] }),
  viewer: fromCasbin({
    roles: ["viewer"],
    policies: [
      ["viewer", "tasks", "read"],
      ["viewer", "settings", "read"],
    ],
  }),
}

/**
 * A small app on the dynamic shell: the rail is set once, and each page
 * brings its own sub nav, header, toolbar, actions and footer.
 */
export function DynamicShellPreview({ className }: { className?: string }) {
  const [path, setPath] = React.useState("/tasks")
  const [role, setRole] = React.useState("admin")
  return (
    <div className="flex min-w-0 flex-col gap-3">
      <div className="flex items-center gap-3 text-2xs text-muted-foreground">
        <Icon icon="team" size={14} />
        Signed in as
        <PreferenceOptions
          label="Signed in as"
          value={role}
          onValueChange={setRole}
          options={[
            { value: "admin", label: "Admin" },
            { value: "viewer", label: "Viewer" },
          ]}
        />
        <span className="ml-auto font-mono">{path}</span>
      </div>
      <UserAccessProvider
        key={role}
        initial={access[role]}
        load={async () => access[role]!}
      >
        <ShellProvider initial={initial} currentPath={path} navigate={setPath}>
          <ShellLayout
            className={className}
            brand={
              <>
                <WorkspaceOrb />
                <SidebarBrandName name="Forge" suffix="Workspace" />
                <SidebarCollapseButton />
              </>
            }
          >
            <Page path={path} navigate={setPath} />
          </ShellLayout>
          <ShellSidebarTop>
            <SidebarSection variant="primary">
              <CommandButton />
            </SidebarSection>
          </ShellSidebarTop>
        </ShellProvider>
      </UserAccessProvider>
    </div>
  )
}
