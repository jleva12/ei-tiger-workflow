import * as React from "react"
import { createPortal } from "react-dom"
import { createStore, useStore, type StoreApi } from "zustand"

import {
  AppShell,
  MainPanel,
  PageContent,
  SkipLink,
  Topbar,
  TopbarActions,
  TopbarBreadcrumb,
  TopbarCrumb,
  TopbarCrumbSeparator,
  TopbarPage,
} from "@/components/forge/app-shell"
import {
  AppMark,
  IconRail,
  RailButton,
  RailFooter,
  RailNav,
} from "@/components/forge/icon-rail"
import {
  NavItem,
  NavSectionHeading,
  SidebarBrand,
  SidebarSection,
  SubNav,
  WorkspaceSidebar,
} from "@/components/forge/workspace-sidebar"
import {
  checkAccess,
  UserAccessContext,
  type UserAccessState,
} from "@/lib/user-access"

import {
  matchActive,
  ShellStoreProvider,
  useShellPage,
  useShellStore,
  useShellStoreApi,
  type ShellConfig,
  type ShellCrumb,
  type ShellLinkRenderer,
  type ShellNavItem,
  type ShellSlotName,
} from "./shell-store"

/* -------------------------------------------------------------------------- */
/* Provider                                                                   */
/* -------------------------------------------------------------------------- */

export type ShellProviderProps = {
  children?: React.ReactNode
  /** The rail, sidebar sub nav and header before any page changes them. */
  initial?: ShellConfig
  /**
   * The current route's path. Rail and sidebar items whose `href` matches
   * (exactly, or as a parent path) are active unless a page says otherwise.
   */
  currentPath?: string
  /**
   * Client-side navigation for items and crumbs with an `href`, e.g. your
   * router's `navigate`. Without it they render as links.
   */
  navigate?: (href: string) => void
  /** Renders `href`s with your router's link: `(href) => <Link to={href} />`. */
  renderLink?: ShellLinkRenderer
}

function SyncRouting({
  currentPath,
  navigate,
  renderLink,
}: Pick<ShellProviderProps, "currentPath" | "navigate" | "renderLink">) {
  const api = useShellStoreApi()
  React.useLayoutEffect(() => {
    api.setState({ currentPath, navigate, renderLink })
  }, [api, currentPath, navigate, renderLink])
  return null
}

/**
 * Holds what the app shell shows — the icon rail, the sidebar sub nav and
 * the header — so pages can change it. Put it above `ShellLayout`; pages
 * use `useShellPage` / `ShellPage`, anything else `useShellApi`.
 */
export function ShellProvider({
  children,
  initial,
  currentPath,
  navigate,
  renderLink,
}: ShellProviderProps) {
  return (
    <ShellStoreProvider
      initial={{ initial, currentPath, navigate, renderLink }}
    >
      <SyncRouting
        currentPath={currentPath}
        navigate={navigate}
        renderLink={renderLink}
      />
      {children}
    </ShellStoreProvider>
  )
}

/* -------------------------------------------------------------------------- */
/* Slots                                                                      */
/* -------------------------------------------------------------------------- */

function SlotOutlet({ name }: { name: ShellSlotName }) {
  const register = useShellStore((state) => state.registerSlot)
  const ref = React.useCallback(
    (element: HTMLDivElement | null) =>
      element ? register(name, element) : undefined,
    [register, name]
  )
  return <div ref={ref} data-slot={`shell-${name}`} className="contents" />
}

/**
 * Renders its children in a region of the shell, from anywhere below
 * `ShellLayout` — with the page's own state and handlers.
 */
export function ShellSlot({
  name,
  children,
}: {
  name: ShellSlotName
  children?: React.ReactNode
}) {
  const target = useShellStore((state) => state.slots[name]?.at(-1))
  return target ? createPortal(children, target) : null
}

/** The page's buttons in the top bar, right-aligned. */
export function ShellHeaderActions({
  children,
}: {
  children?: React.ReactNode
}) {
  return <ShellSlot name="header-actions">{children}</ShellSlot>
}

/** A `ViewToolbar` (tabs, filters) between the top bar and the content. */
export function ShellToolbar({ children }: { children?: React.ReactNode }) {
  return <ShellSlot name="toolbar">{children}</ShellSlot>
}

/** A `WorkspaceFooter` under the content. */
export function ShellFooter({ children }: { children?: React.ReactNode }) {
  return <ShellSlot name="footer">{children}</ShellSlot>
}

/** Sidebar content above the sections, e.g. `CommandButton` or a switcher. */
export function ShellSidebarTop({ children }: { children?: React.ReactNode }) {
  return <ShellSlot name="sidebar-top">{children}</ShellSlot>
}

/** Sidebar content below the sections, e.g. `SidebarStatus`. */
export function ShellSidebarBottom({
  children,
}: {
  children?: React.ReactNode
}) {
  return <ShellSlot name="sidebar-bottom">{children}</ShellSlot>
}

/* -------------------------------------------------------------------------- */
/* Pages                                                                      */
/* -------------------------------------------------------------------------- */

/** `useShellPage` as an element: the shell shows this while it's mounted. */
export function ShellPage(config: ShellConfig) {
  useShellPage(config)
  return null
}

/* -------------------------------------------------------------------------- */
/* Rendering                                                                  */
/* -------------------------------------------------------------------------- */

// Without a UserAccessProvider there's nothing to check, so nothing hides.
const noAccess = createStore<UserAccessState | null>(() => null)

function useItemFilter() {
  const store = React.useContext(
    UserAccessContext
  ) as StoreApi<UserAccessState | null> | null
  const access = useStore(store ?? noAccess, (state) => state)
  return React.useCallback(
    function filter(items: readonly ShellNavItem[]): ShellNavItem[] {
      return items
        .filter((item) => {
          if (!item.permission && !item.role) return true
          if (!store || !access) return true
          return (
            access.status === "ready" &&
            checkAccess(access, {
              permission: item.permission,
              role: item.role,
            })
          )
        })
        .map((item) =>
          item.items ? { ...item, items: filter(item.items) } : item
        )
    },
    [store, access]
  )
}

type Routing = {
  navigate: ((href: string) => void) | undefined
  renderLink: ShellLinkRenderer | undefined
}

/** Props that make a control go to `href`: a click handler or a link. */
function linkProps(
  target: { href?: string; onSelect?: () => void },
  { navigate, renderLink }: Routing
) {
  const onClick = () => {
    target.onSelect?.()
    if (target.href && navigate) navigate(target.href)
  }
  if (!target.href || navigate) return { onClick }
  return {
    onClick,
    render: renderLink ? renderLink(target.href) : <a href={target.href} />,
  }
}

function ShellNavItems({
  items,
  activeId,
  routing,
  size,
}: {
  items: readonly ShellNavItem[]
  activeId: string | undefined
  routing: Routing
  size?: "default" | "sm"
}) {
  return items.map((item) => (
    <React.Fragment key={item.id}>
      <NavItem
        icon={item.icon}
        active={item.id === activeId}
        meta={item.meta}
        size={size}
        {...linkProps(item, routing)}
      >
        {item.label}
      </NavItem>
      {item.items && item.items.length > 0 && (
        <SubNav>
          <ShellNavItems
            items={item.items}
            activeId={activeId}
            routing={routing}
            size="sm"
          />
        </SubNav>
      )}
    </React.Fragment>
  ))
}

function ShellRailButton({
  item,
  active,
  routing,
}: {
  item: ShellNavItem
  active: boolean
  routing: Routing
}) {
  const { render, onClick } = linkProps(item, routing)
  return (
    <RailButton
      icon={item.icon ?? "layers"}
      label={item.label}
      active={active}
      onClick={onClick}
      {...(render && { render, nativeButton: false })}
    />
  )
}

function Crumb({ crumb, routing }: { crumb: ShellCrumb; routing: Routing }) {
  return (
    <>
      <TopbarCrumb
        icon={crumb.icon}
        onClick={() => {
          crumb.onSelect?.()
          if (!crumb.href) return
          if (routing.navigate) routing.navigate(crumb.href)
          else window.location.assign(crumb.href)
        }}
      >
        {crumb.label}
      </TopbarCrumb>
      <TopbarCrumbSeparator />
    </>
  )
}

export type ShellLayoutProps = {
  /** The page. Rendered in the scrolling `PageContent` (see `bare`). */
  children?: React.ReactNode
  /** The sidebar's brand row, e.g. orb, name and collapse button. */
  brand?: React.ReactNode
  /** The rail's top mark. Default: `AppMark`, going to the first rail item. */
  mark?: React.ReactNode
  /** Render the page without `PageContent`, for pages with their own scroll region. */
  bare?: boolean
  /** Classes for `AppShell` (e.g. `h-[640px]` to embed it). */
  className?: string
}

/**
 * The Forge app shell drawn from `ShellProvider`: icon rail, sidebar sub nav
 * with its sections, and the top bar with breadcrumbs, the page title and
 * the page's actions. Active items follow the current path; items with a
 * `permission` or `role` hide for users without it.
 */
export function ShellLayout({
  children,
  brand,
  mark,
  bare = false,
  className,
}: ShellLayoutProps) {
  const shell = useShellStore((state) => state.shell)
  const currentPath = useShellStore((state) => state.currentPath)
  const navigate = useShellStore((state) => state.navigate)
  const renderLink = useShellStore((state) => state.renderLink)
  const routing = React.useMemo(
    () => ({ navigate, renderLink }),
    [navigate, renderLink]
  )
  const filter = useItemFilter()

  const railItems = filter(shell.rail.items)
  const railFooter = filter(shell.rail.footer)
  const activeRail =
    shell.active.rail ?? matchActive([...railItems, ...railFooter], currentPath)
  const sections = shell.sidebar.sections
    .map((section) => ({ ...section, items: filter(section.items) }))
    .filter((section) => section.items.length > 0)
  const activeSidebar =
    shell.active.sidebar ??
    matchActive(
      sections.flatMap((section) => section.items),
      currentPath
    )
  const sidebarHidden = shell.sidebar.hidden
  const { title, icon, breadcrumbs } = shell.header

  return (
    <AppShell className={className}>
      <SkipLink href="#content">Skip to content</SkipLink>
      <IconRail>
        {mark ?? (
          <AppMark
            aria-label={railItems[0]?.label ?? "Home"}
            onClick={() => {
              const home = railItems[0]
              home?.onSelect?.()
              if (!home?.href) return
              if (navigate) navigate(home.href)
              else window.location.assign(home.href)
            }}
          />
        )}
        <RailNav aria-label="Sections">
          {railItems.map((item) => (
            <ShellRailButton
              key={item.id}
              item={item}
              active={item.id === activeRail}
              routing={routing}
            />
          ))}
        </RailNav>
        {railFooter.length > 0 && (
          <RailFooter>
            {railFooter.map((item) => (
              <ShellRailButton
                key={item.id}
                item={item}
                active={item.id === activeRail}
                routing={routing}
              />
            ))}
          </RailFooter>
        )}
      </IconRail>
      <WorkspaceSidebar
        label={shell.sidebar.label ?? "Navigation"}
        // A hidden sub nav still backs the mobile menu, for the rail's items.
        className={sidebarHidden ? "hidden" : undefined}
      >
        {brand && <SidebarBrand>{brand}</SidebarBrand>}
        <SlotOutlet name="sidebar-top" />
        {railItems.length > 0 && (
          // The rail hides on phones; its items lead the mobile menu instead.
          <SidebarSection
            variant="primary"
            className={sidebarHidden ? undefined : "hidden max-[600px]:block"}
          >
            <ShellNavItems
              items={[...railItems, ...railFooter]}
              activeId={activeRail}
              routing={routing}
            />
          </SidebarSection>
        )}
        {!sidebarHidden &&
          sections.map((section, index) => (
            <SidebarSection
              key={section.id}
              variant={
                section.variant ??
                (index === sections.length - 1 ? "flush" : "default")
              }
            >
              {section.title && (
                <NavSectionHeading action={section.action}>
                  {section.title}
                </NavSectionHeading>
              )}
              <ShellNavItems
                items={section.items}
                activeId={activeSidebar}
                routing={routing}
              />
            </SidebarSection>
          ))}
        <SlotOutlet name="sidebar-bottom" />
      </WorkspaceSidebar>
      <MainPanel>
        <Topbar>
          <TopbarBreadcrumb>
            {breadcrumbs.map((crumb, index) => (
              <Crumb key={crumb.id ?? index} crumb={crumb} routing={routing} />
            ))}
            {title && <TopbarPage icon={icon}>{title}</TopbarPage>}
          </TopbarBreadcrumb>
          <TopbarActions>
            <SlotOutlet name="header-actions" />
          </TopbarActions>
        </Topbar>
        <SlotOutlet name="toolbar" />
        {bare ? children : <PageContent id="content">{children}</PageContent>}
        <SlotOutlet name="footer" />
      </MainPanel>
    </AppShell>
  )
}
