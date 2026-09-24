import * as React from "react"

import { Button } from "@/components/ui/button"
import { useTheme } from "@/components/theme-provider"
import {
  AppShell,
  FooterShortcut,
  MainPanel,
  PageContent,
  SkipLink,
  Topbar,
  TopbarActions,
  TopbarBreadcrumb,
  TopbarCrumb,
  TopbarCrumbSeparator,
  TopbarPage,
  WorkspaceFooter,
} from "@/components/forge/app-shell"
import { WorkspaceOrb } from "@/components/forge/avatars"
import { Icon } from "@/components/forge/icon"
import {
  AppMark,
  IconRail,
  RailButton,
  RailFooter,
  RailNav,
} from "@/components/forge/icon-rail"
import { ConnectionDot } from "@/components/forge/status"
import { SearchField } from "@/components/forge/toolbar"
import {
  CommandButton,
  NavItem,
  NavSectionHeading,
  SidebarBrand,
  SidebarBrandName,
  SidebarCollapseButton,
  SidebarHint,
  SidebarSection,
  SidebarStatus,
  WorkspaceSidebar,
} from "@/components/forge/workspace-sidebar"
import { InstallDialog } from "@/demo/install-dialog"
import { PreferencesSection } from "@/demo/sections/preferences"
import { AccessSection } from "@/demo/sections/access"
import { DynamicShellSection } from "@/demo/sections/dynamic-shell"
import { DynamicShellPreview } from "@/demo/dynamic-shell-preview"
import { PreferencesPanel } from "@/components/forge/preferences"
import {
  Popover,
  PopoverContent,
  PopoverDescription,
  PopoverHeader,
  PopoverTitle,
  PopoverTrigger,
} from "@/components/ui/popover"
import { WorkspacePreview } from "@/demo/workspace-preview"
import {
  AssistantModalPreview,
  AssistantPreview,
} from "@/demo/assistant-preview"
import {
  isSectionId,
  sectionGroups,
  sections,
  type SectionGroup,
  type SectionId,
} from "@/demo/nav"
import {
  ChangesSection,
  HandoffSection,
  MetricsSection,
  RunLogsSection,
  SubmissionSection,
  TaskPageSection,
} from "@/demo/sections/detail"
import { AssistantSection } from "@/demo/sections/assistant"
import { DataTableSection } from "@/demo/sections/data-table"
import { IntegrationsSection } from "@/demo/sections/integrations"
import {
  ColorsSection,
  IconsSection,
  OverviewSection,
  RadiiSection,
  TypographySection,
} from "@/demo/sections/foundations"
import {
  BadgesSection,
  ButtonsSection,
  FeedbackSection,
  InputsSection,
  MenusSection,
  TabsSection,
} from "@/demo/sections/primitives"
import {
  ActivitySection,
  EmptySection,
  KanbanSection,
  NavigationSection,
  SheetsSection,
  ShellSection,
  TaskListSection,
} from "@/demo/sections/workspace"

function readHash(): SectionId {
  const id = window.location.hash.replace(/^#\/?/, "")
  return isSectionId(id) ? id : "overview"
}

/**
 * `#workspace` renders the rebuilt Forge Workspace on its own, full screen;
 * `#assistant-app` does the same for the AI assistant, and
 * `#assistant-modal-app` shows the workspace with the floating assistant, and
 * `#dynamic-shell-app` the dynamic shell demo.
 */
function useFullscreenWorkspace() {
  const read = () =>
    window.location.hash === "#workspace"
      ? "workspace"
      : window.location.hash === "#assistant-app"
        ? "assistant"
        : window.location.hash === "#assistant-modal-app"
          ? "assistant-modal"
          : window.location.hash === "#dynamic-shell-app"
            ? "dynamic-shell"
            : null
  const [fullscreen, setFullscreen] = React.useState(read)
  React.useEffect(() => {
    const sync = () => setFullscreen(read())
    window.addEventListener("hashchange", sync)
    return () => window.removeEventListener("hashchange", sync)
  }, [])
  return fullscreen
}

/** The current section lives in the URL hash so pages are linkable. */
function useHashSection() {
  const [section, setSection] = React.useState<SectionId>(readHash)
  React.useEffect(() => {
    const sync = () => setSection(readHash())
    window.addEventListener("hashchange", sync)
    return () => window.removeEventListener("hashchange", sync)
  }, [])
  const navigate = React.useCallback((id: SectionId) => {
    if (window.location.hash === `#${id}`) setSection(id)
    else window.location.hash = id
  }, [])
  return [section, navigate] as const
}

const firstInGroup: Record<SectionGroup, SectionId> = {
  Foundations: "colors",
  Primitives: "buttons",
  Workspace: "shell",
  "Detail pages": "task-page",
}

export function App() {
  const fullscreen = useFullscreenWorkspace()
  if (fullscreen === "workspace") return <WorkspacePreview />
  if (fullscreen === "assistant") return <AssistantPreview />
  if (fullscreen === "dynamic-shell") {
    return <DynamicShellPreview className="h-[calc(100dvh-3rem)] min-w-0" />
  }
  if (fullscreen === "assistant-modal") {
    return <AssistantModalPreview className="h-dvh" defaultOpen />
  }
  return <DesignSystem />
}

function DesignSystem() {
  const [section, navigate] = useHashSection()
  const { theme, setTheme } = useTheme()
  const [query, setQuery] = React.useState("")
  const search = React.useRef<HTMLInputElement>(null)
  const content = React.useRef<HTMLDivElement>(null)
  const current = sections.find((item) => item.id === section) ?? sections[0]
  const matches = sections.filter((item) =>
    `${item.label} ${item.group}`
      .toLowerCase()
      .includes(query.trim().toLowerCase())
  )
  const dark = theme === "dark"
  const toggleTheme = () => setTheme(dark ? "light" : "dark")

  React.useEffect(() => {
    content.current?.scrollTo({ top: 0 })
  }, [section])

  React.useEffect(() => {
    function shortcuts(event: KeyboardEvent) {
      const editable =
        event.target instanceof HTMLElement &&
        Boolean(
          event.target.closest(
            "input, textarea, select, [contenteditable=true]"
          )
        )
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault()
        search.current?.focus()
      } else if (!editable && event.key === "/") {
        event.preventDefault()
        search.current?.focus()
      }
    }
    window.addEventListener("keydown", shortcuts)
    return () => window.removeEventListener("keydown", shortcuts)
  }, [])

  return (
    <AppShell>
      <SkipLink href="#content">Skip to content</SkipLink>
      <IconRail>
        <AppMark
          aria-label="Design system overview"
          onClick={() => navigate("overview")}
        />
        <RailNav aria-label="Section groups">
          <RailButton
            icon="home"
            label="Overview"
            active={section === "overview"}
            onClick={() => navigate("overview")}
          />
          {sectionGroups.map((group) => {
            const first = sections.find(
              (item) => item.id === firstInGroup[group]
            )!
            return (
              <RailButton
                key={group}
                icon={first.icon}
                label={group}
                active={current.group === group && section !== "overview"}
                onClick={() => navigate(firstInGroup[group])}
              />
            )
          })}
          <RailButton
            icon="search"
            label="Find a component"
            onClick={() => search.current?.focus()}
          />
        </RailNav>
        <RailFooter>
          <RailButton
            icon={dark ? "moon" : "sun"}
            label="Toggle color theme"
            onClick={toggleTheme}
          />
          <WorkspaceOrb size="lg" className="mt-[7px]" />
        </RailFooter>
      </IconRail>

      <WorkspaceSidebar label="Component navigation">
        <SidebarBrand>
          <WorkspaceOrb />
          <SidebarBrandName name="Forge" suffix="UI" />
          <SidebarCollapseButton />
        </SidebarBrand>
        <SidebarSection variant="primary">
          <CommandButton onClick={() => search.current?.focus()}>
            Find a component
          </CommandButton>
          <NavItem
            icon="home"
            active={section === "overview"}
            onClick={() => navigate("overview")}
          >
            Overview
          </NavItem>
          <NavItem
            icon="view"
            active={section === "shell"}
            onClick={() => navigate("shell")}
            meta={<ConnectionDot />}
          >
            Live demo
          </NavItem>
        </SidebarSection>
        {sectionGroups.map((group, index) => {
          const items = matches.filter(
            (item) => item.group === group && item.id !== "overview"
          )
          if (!items.length) return null
          return (
            <SidebarSection
              key={group}
              variant={index === sectionGroups.length - 1 ? "flush" : "default"}
            >
              <NavSectionHeading>{group}</NavSectionHeading>
              {items.map((item) => (
                <NavItem
                  key={item.id}
                  icon={item.icon}
                  active={item.id === section}
                  onClick={() => navigate(item.id)}
                >
                  {item.label}
                </NavItem>
              ))}
            </SidebarSection>
          )
        })}
        {!matches.length && (
          <SidebarSection variant="flush">
            <SidebarHint>No components match “{query}”.</SidebarHint>
          </SidebarSection>
        )}
        <SidebarStatus>Tokens synced with Forge Workspace</SidebarStatus>
      </WorkspaceSidebar>

      <MainPanel>
        <Topbar>
          <TopbarBreadcrumb>
            <TopbarCrumb icon="layers" onClick={() => navigate("overview")}>
              Design system
            </TopbarCrumb>
            <TopbarCrumbSeparator />
            <TopbarPage icon={current.icon}>{current.label}</TopbarPage>
          </TopbarBreadcrumb>
          <TopbarActions>
            <SearchField
              ref={search}
              aria-label="Find a component"
              placeholder="Find component"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && matches[0]) {
                  navigate(matches[0].id)
                  setQuery("")
                  event.currentTarget.blur()
                }
                if (event.key === "Escape") {
                  setQuery("")
                  event.currentTarget.blur()
                }
              }}
            />
            <Popover>
              <PopoverTrigger
                render={
                  <Button
                    variant="outline"
                    size="icon"
                    aria-label="Display settings"
                  />
                }
              >
                <Icon icon="settings" />
              </PopoverTrigger>
              <PopoverContent align="end" className="w-[26rem] gap-4 p-4">
                <PopoverHeader>
                  <PopoverTitle>Display settings</PopoverTitle>
                  <PopoverDescription>
                    Saved in this browser, applied everywhere.
                  </PopoverDescription>
                </PopoverHeader>
                <PreferencesPanel showTheme={false} />
              </PopoverContent>
            </Popover>
            <Button
              variant="outline"
              size="icon"
              aria-label="Toggle color theme"
              onClick={toggleTheme}
            >
              <Icon icon={dark ? "moon" : "sun"} />
            </Button>
            <InstallDialog />
          </TopbarActions>
        </Topbar>
        <PageContent id="content" ref={content}>
          <SectionView section={section} onNavigate={navigate} />
        </PageContent>
        <WorkspaceFooter>
          <span>
            <ConnectionDot />
            Forge Workspace design system
          </span>
          <span>shadcn/ui · Base UI · Tailwind v4</span>
          <FooterShortcut keys="D">Toggle theme</FooterShortcut>
        </WorkspaceFooter>
      </MainPanel>
    </AppShell>
  )
}

function SectionView({
  section,
  onNavigate,
}: {
  section: SectionId
  onNavigate: (id: SectionId) => void
}) {
  switch (section) {
    case "overview":
      return <OverviewSection onNavigate={onNavigate} />
    case "colors":
      return <ColorsSection />
    case "typography":
      return <TypographySection />
    case "radii":
      return <RadiiSection />
    case "icons":
      return <IconsSection />
    case "preferences":
      return <PreferencesSection />
    case "access":
      return <AccessSection />
    case "dynamic-shell":
      return <DynamicShellSection />
    case "buttons":
      return <ButtonsSection />
    case "inputs":
      return <InputsSection />
    case "menus":
      return <MenusSection />
    case "tabs":
      return <TabsSection />
    case "badges":
      return <BadgesSection />
    case "feedback":
      return <FeedbackSection />
    case "shell":
      return <ShellSection />
    case "assistant":
      return <AssistantSection />
    case "navigation":
      return <NavigationSection />
    case "task-list":
      return <TaskListSection />
    case "data-table":
      return <DataTableSection />
    case "kanban":
      return <KanbanSection />
    case "empty":
      return <EmptySection />
    case "activity":
      return <ActivitySection />
    case "sheets":
      return <SheetsSection />
    case "integrations":
      return <IntegrationsSection />
    case "task-page":
      return <TaskPageSection />
    case "submission":
      return <SubmissionSection />
    case "run-logs":
      return <RunLogsSection />
    case "changes":
      return <ChangesSection />
    case "handoff":
      return <HandoffSection />
    case "metrics":
      return <MetricsSection />
  }
}

export default App
