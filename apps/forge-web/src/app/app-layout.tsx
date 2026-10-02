import * as React from "react"
import { Outlet, useLocation, useNavigate } from "@tanstack/react-router"

import { WorkspaceOrb } from "@/components/forge/avatars"
import {
  ShellLayout,
  ShellProvider,
  ShellSidebarBottom,
  ShellSidebarHeader,
  type ShellConfig,
} from "@/components/forge/shell/index"
import {
  SidebarBrandName,
  SidebarCollapseButton,
} from "@/components/forge/workspace-sidebar"
import { AppAssistant } from "@/features/assistant/components/app-assistant"
import { AppFooter } from "@/app/app-footer"
import { NavUser } from "@/app/nav-user"
import { ScopeSwitcher } from "@/app/scope-switcher"
import { SettingsDialog } from "@/app/settings/settings-dialog"
import { useMyOrganizations } from "@/lib/access"
import { AssistantLauncherProvider } from "@/features/assistant/lib/assistant-launcher"
import { PageContextProvider } from "@/features/assistant/lib/page-context"
import { useOpenSettings } from "@/app/settings/settings"
import { useHasRole } from "@/lib/user-access"
import {
  SITE_ADMIN,
  WorkspaceScopeProvider,
  isAdminPath,
  organizationInPath,
  useWorkspaceScope,
} from "@/app/workspace-scope"

/**
 * The shell before any page changes it; pages add to it with `useShellPage`.
 * Its sub nav is empty: each scope draws its own (an organization's page, site
 * administration's layout) and error pages draw the one of the scope you're
 * in (`ScopeNav`), so a page never shows another scope's. The admin rail
 * items only show for site administrators (`role`). Settings opens as a
 * dialog over the page, not a page.
 */
const appShell = (openSettings: () => void): ShellConfig => ({
  rail: {
    items: [
      { id: "home", label: "Home", icon: "home", href: "/" },
      {
        id: "organizations",
        label: "Organizations",
        icon: "layers",
        href: "/admin/organizations",
        role: SITE_ADMIN,
      },
      {
        id: "users",
        label: "Users",
        icon: "team",
        href: "/admin/users",
        role: SITE_ADMIN,
      },
    ],
    footer: [
      {
        id: "settings",
        label: "Settings",
        icon: "settings",
        onSelect: openSettings,
      },
    ],
  },
})

/**
 * Opening a scope's page makes it the scope you're working in: the
 * workspace of an organization you're a member of, or (for a site administrator) a
 * site administration page.
 */
function ScopeFromRoute({ pathname }: { pathname: string }) {
  const setScope = useWorkspaceScope((state) => state.setScope)
  const isAdmin = useHasRole(SITE_ADMIN)
  const myOrganizations = useMyOrganizations()
  React.useEffect(() => {
    const organization = organizationInPath(pathname)
    if (organization) {
      if (myOrganizations.data?.some((mine) => mine.id === organization))
        setScope(`org:${organization}`)
    } else if (isAdmin && isAdminPath(pathname)) setScope("admin")
  }, [pathname, isAdmin, myOrganizations.data, setScope])
  return null
}

/** The Forge app shell, driven by the router: active items follow the URL. */
export function AppLayout() {
  const navigate = useNavigate()
  const pathname = useLocation({ select: (location) => location.pathname })
  const go = React.useCallback(
    (href: string) => void navigate({ href }),
    [navigate]
  )
  const openSettings = useOpenSettings()
  // The shell reads its first config once; opening Settings is stable.
  const [initial] = React.useState(() => appShell(() => openSettings()))

  return (
    <WorkspaceScopeProvider>
      <ScopeFromRoute pathname={pathname} />
      {/* What pages tell the assistant they show (usePageContext), and
          whether they have it built in (useHideAssistantLauncher). */}
      <PageContextProvider>
        <AssistantLauncherProvider>
          <ShellProvider initial={initial} currentPath={pathname} navigate={go}>
            <ShellLayout
              brand={
                <>
                  <WorkspaceOrb />
                  <SidebarBrandName name="Forge" suffix="Workspace" />
                  <SidebarCollapseButton />
                </>
              }
            >
              <ShellSidebarHeader>
                <ScopeSwitcher />
              </ShellSidebarHeader>
              <ShellSidebarBottom>
                <NavUser />
              </ShellSidebarBottom>
              <AppFooter />
              <AppAssistant />
              <SettingsDialog />
              <Outlet />
            </ShellLayout>
          </ShellProvider>
        </AssistantLauncherProvider>
      </PageContextProvider>
    </WorkspaceScopeProvider>
  )
}
