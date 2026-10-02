import * as React from "react"

import { Button } from "@/components/ui/button"
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { Tabs } from "@/components/ui/tabs"
import { toast } from "@/components/ui/toast"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { useTheme } from "@/components/theme-provider"
import {
  EventItem,
  EventList,
  Footnote,
  Footnotes,
  Stat,
  StatGrid,
  StatusHeading,
  ViewHeading,
  WorkspaceView,
} from "@/components/forge/activity"
import {
  AppShell,
  FooterShortcut,
  MainPanel,
  PageContent,
  PrimaryAction,
  ToolbarFilters,
  Topbar,
  TopbarActions,
  TopbarAgents,
  TopbarBreadcrumb,
  TopbarCrumb,
  TopbarCrumbSeparator,
  TopbarPage,
  ViewToolbar,
  WorkspaceFooter,
} from "@/components/forge/app-shell"
import { AgentAvatars, WorkspaceOrb } from "@/components/forge/avatars"
import { EmptyIllustration, EmptyWorkspace } from "@/components/forge/empty-state"
import { ActiveFilters, PreviewBanner } from "@/components/forge/feedback"
import { Icon } from "@/components/forge/icon"
import {
  AppMark,
  IconRail,
  RailButton,
  RailFooter,
  RailNav,
} from "@/components/forge/icon-rail"
import { TaskBoard } from "@/components/forge/kanban"
import { ConnectionDot } from "@/components/forge/status"
import {
  TaskGroup,
  TaskGroupAction,
  TaskList,
  TaskTable,
  type TaskItem,
} from "@/components/forge/task-list"
import {
  LayoutSwitch,
  SearchField,
  ToolbarButton,
  ViewTabsList,
  ViewTabsTrigger,
} from "@/components/forge/toolbar"
import {
  CommandButton,
  NavItem,
  NavSectionHeading,
  ProjectItem,
  SidebarBrand,
  SidebarBrandAction,
  SidebarBrandName,
  SidebarCollapseButton,
  SidebarSection,
  SidebarStatus,
  SubNav,
  WorkspaceSidebar,
} from "@/components/forge/workspace-sidebar"
import { CreateTaskSheet } from "./create-task-sheet"
import { agents, events, groupTasks, tasks } from "./data"
import { TaskDetailPreview } from "./detail-previews"

const views = [
  { id: "list", label: "List", icon: "list" },
  { id: "kanban", label: "Kanban", icon: "kanban" },
  { id: "activity", label: "Activity", icon: "activity" },
  { id: "dashboard", label: "Dashboard", icon: "dashboard" },
] as const

const repositories = ["web", "admin-api", "coding-agent", "common"]

/**
 * A complete recreation of the Forge Workspace task dashboard, composed only
 * from library components. Interactive: views, search, grouping, density,
 * collapsing groups, task detail pages and the create-task sheet all work.
 */
export function WorkspacePreview({ className }: { className?: string }) {
  const { theme, setTheme } = useTheme()
  const [view, setView] = React.useState<string>("list")
  const [query, setQuery] = React.useState("")
  const [groupBy, setGroupBy] = React.useState("status")
  const [sort, setSort] = React.useState("updated")
  const [compact, setCompact] = React.useState(false)
  const [hideEmpty, setHideEmpty] = React.useState(false)
  const [repo, setRepo] = React.useState("all")
  const [viewsOpen, setViewsOpen] = React.useState(false)
  const [creating, setCreating] = React.useState(false)
  const [selected, setSelected] = React.useState<TaskItem | null>(null)
  const search = React.useRef<HTMLInputElement>(null)

  const visible = tasks
    .filter(
      (task) =>
        (repo === "all" || task.repository === `workspace/${repo}`) &&
        `${task.id} ${task.title}`
          .toLowerCase()
          .includes(query.trim().toLowerCase())
    )
    .sort((a, b) =>
      sort === "name"
        ? a.title.localeCompare(b.title)
        : sort === "oldest"
          ? a.id.localeCompare(b.id)
          : b.id.localeCompare(a.id)
    )
  const filtered = query.trim() !== "" || repo !== "all"
  const sections =
    groupBy === "repository"
      ? repositories.map((name) => ({
          id: name,
          title: `workspace/${name}`,
          symbol: "backlog" as const,
          tasks: visible.filter(
            (task) => task.repository === `workspace/${name}`
          ),
        }))
      : groupBy === "none"
        ? [
            {
              id: "all",
              title: "All tasks",
              symbol: "backlog" as const,
              tasks: visible,
            },
          ]
        : groupTasks(visible)
  const shownSections = sections.filter(
    (section) => !hideEmpty || section.tasks.length
  )

  function selectView(next: string) {
    setSelected(null)
    setView(next)
  }
  function resetFilters() {
    setQuery("")
    setRepo("all")
  }

  return (
    <AppShell className={className}>
      <IconRail>
        <AppMark
          aria-label="Forge task workspace"
          onClick={() => selectView("list")}
        />
        <RailNav aria-label="Shortcuts">
          <RailButton
            icon="home"
            label="Task overview"
            active={view === "list" && !selected}
            onClick={() => selectView("list")}
          />
          <RailButton
            icon="search"
            label="Search tasks"
            onClick={() => search.current?.focus()}
          />
          <RailButton
            icon="bell"
            label="Activity"
            active={view === "activity" && !selected}
            onClick={() => selectView("activity")}
          />
          <RailButton icon="folder" label="Projects" />
          <RailButton
            icon="task"
            label="New task"
            onClick={() => setCreating(true)}
          />
          <RailButton icon="team" label="Agent plans" />
          <RailButton
            icon="dashboard"
            label="Queue dashboard"
            active={view === "dashboard" && !selected}
            onClick={() => selectView("dashboard")}
          />
        </RailNav>
        <RailFooter>
          <RailButton
            icon={theme === "dark" ? "moon" : "sun"}
            label="Toggle color theme"
            onClick={() => setTheme(theme === "dark" ? "light" : "dark")}
          />
          <RailButton icon="settings" label="Settings" />
          <WorkspaceOrb
            size="lg"
            className="mt-[7px]"
            render={<button type="button" aria-label="Switch workspace" />}
          />
        </RailFooter>
      </IconRail>

      <WorkspaceSidebar>
        <SidebarBrand>
          <WorkspaceOrb />
          <SidebarBrandName name="Forge" suffix="Workspace" />
          <DropdownMenu>
            <DropdownMenuTrigger
              render={<SidebarBrandAction aria-label="Workspace options" />}
            >
              <Icon icon="down" size={14} />
            </DropdownMenuTrigger>
            <DropdownMenuContent className="min-w-52">
              <DropdownMenuGroup>
                <DropdownMenuItem>
                  <Icon icon="layers" />
                  Switch to live tasks
                </DropdownMenuItem>
                <DropdownMenuItem>
                  <Icon icon="robot" />
                  View agent plans
                </DropdownMenuItem>
              </DropdownMenuGroup>
            </DropdownMenuContent>
          </DropdownMenu>
          <SidebarCollapseButton />
        </SidebarBrand>
        <SidebarSection variant="primary">
          <CommandButton onClick={() => search.current?.focus()} />
          <NavItem icon="home" onClick={() => selectView("dashboard")}>
            Home
          </NavItem>
          <NavItem
            icon="bell"
            active={view === "activity" && !selected}
            meta={<ConnectionDot online={false} />}
            onClick={() => selectView("activity")}
          >
            Updates
          </NavItem>
          <NavItem icon="activity" meta={2} onClick={() => selectView("list")}>
            Active tasks
          </NavItem>
          <NavItem
            icon="task"
            meta={<Icon icon="plus" size={14} />}
            onClick={() => setCreating(true)}
          >
            New task
          </NavItem>
        </SidebarSection>
        <SidebarSection>
          <NavSectionHeading
            action={
              <Button
                variant="ghost"
                size="icon-xs"
                aria-label="Create task"
                onClick={() => setCreating(true)}
              >
                <Icon icon="plus" size={14} />
              </Button>
            }
          >
            Workspace
          </NavSectionHeading>
          <NavItem icon="layers" meta={<Icon icon="down" size={13} />}>
            Projects
          </NavItem>
          <NavItem
            icon="task"
            active={(view === "list" || view === "kanban") && !selected}
            meta={tasks.length}
            onClick={() => {
              resetFilters()
              selectView("list")
            }}
          >
            Tasks
          </NavItem>
          <NavItem
            icon="dashboard"
            aria-expanded={viewsOpen}
            meta={<Icon icon={viewsOpen ? "down" : "right"} size={13} />}
            onClick={() => setViewsOpen((open) => !open)}
          >
            Views
          </NavItem>
          {viewsOpen && (
            <SubNav>
              {views.map((item) => (
                <NavItem
                  key={item.id}
                  size="sm"
                  icon={item.icon}
                  onClick={() => selectView(item.id)}
                >
                  {item.label}
                </NavItem>
              ))}
            </SubNav>
          )}
          <NavItem icon="team">Agent plans</NavItem>
          <NavItem
            icon="list"
            active={view === "dashboard" && !selected}
            onClick={() => selectView("dashboard")}
          >
            Queue health
          </NavItem>
        </SidebarSection>
        <SidebarSection variant="flush">
          <NavSectionHeading
            action={
              <Button
                variant="ghost"
                size="icon-xs"
                aria-label="Add a repository"
              >
                <Icon icon="plus" size={14} />
              </Button>
            }
          >
            Projects
          </NavSectionHeading>
          {repositories.map((name, index) => (
            <ProjectItem
              key={name}
              color={index}
              active={repo === name}
              onClick={() => {
                setRepo(repo === name ? "all" : name)
                selectView("list")
              }}
            >
              {name}
            </ProjectItem>
          ))}
        </SidebarSection>
        <SidebarStatus online={false} icon="layers">
          Sample workspace
        </SidebarStatus>
      </WorkspaceSidebar>

      <MainPanel>
        <Topbar>
          <TopbarBreadcrumb>
            <TopbarCrumb icon="task" onClick={() => setSelected(null)}>
              Tasks
            </TopbarCrumb>
            <TopbarCrumbSeparator />
            <TopbarPage icon="dashboard">
              {selected ? selected.id : "Coding Tasks"}
            </TopbarPage>
          </TopbarBreadcrumb>
          <DropdownMenu>
            <DropdownMenuTrigger
              render={
                <Button
                  variant="ghost"
                  size="icon-xs"
                  aria-label="Task workspace options"
                  className="@max-[600px]/shell:hidden"
                />
              }
            >
              <Icon icon="more" />
            </DropdownMenuTrigger>
            <DropdownMenuContent>
              <DropdownMenuGroup>
                <DropdownMenuItem
                  onClick={() =>
                    toast.add({ title: "Dashboard link copied.", type: "info" })
                  }
                >
                  Copy dashboard link
                </DropdownMenuItem>
              </DropdownMenuGroup>
            </DropdownMenuContent>
          </DropdownMenu>
          <TopbarActions>
            <TopbarAgents>
              <AgentAvatars agents={agents} size="lg" />
            </TopbarAgents>
            <LayoutSwitch
              value={view === "kanban" ? "kanban" : "list"}
              onValueChange={selectView}
              options={[
                { value: "kanban", label: "Kanban layout", icon: "kanban" },
                { value: "list", label: "List layout", icon: "list" },
              ]}
            />
            <SearchField
              ref={search}
              aria-label="Search tasks"
              value={query}
              onChange={(event) => {
                setQuery(event.target.value)
                setSelected(null)
                if (view !== "list" && view !== "kanban") setView("list")
              }}
            />
            <PrimaryAction onClick={() => setCreating(true)}>
              Task
            </PrimaryAction>
          </TopbarActions>
        </Topbar>

        {selected ? (
          <TaskDetailPreview task={selected} onBack={() => setSelected(null)} />
        ) : (
          <>
            <ViewToolbar>
              <Tabs
                value={view}
                onValueChange={(value) => selectView(String(value))}
              >
                <ViewTabsList aria-label="Task views">
                  {views.map((item) => (
                    <ViewTabsTrigger
                      key={item.id}
                      value={item.id}
                      icon={item.icon}
                    >
                      {item.label}
                    </ViewTabsTrigger>
                  ))}
                </ViewTabsList>
              </Tabs>
              <DropdownMenu>
                <DropdownMenuTrigger
                  render={
                    <ToolbarButton
                      icon="plus"
                      className="@max-[1270px]/shell:hidden"
                    />
                  }
                >
                  View
                </DropdownMenuTrigger>
                <DropdownMenuContent>
                  <DropdownMenuGroup>
                    {views.map((item) => (
                      <DropdownMenuItem
                        key={item.id}
                        onClick={() => selectView(item.id)}
                      >
                        <Icon icon={item.icon} />
                        {item.label}
                      </DropdownMenuItem>
                    ))}
                  </DropdownMenuGroup>
                </DropdownMenuContent>
              </DropdownMenu>
              <ToolbarFilters>
                {(view === "list" || view === "kanban") && (
                  <>
                    <DropdownMenu>
                      <DropdownMenuTrigger
                        render={
                          <ToolbarButton
                            icon="dashboard"
                            value={
                              groupBy === "status"
                                ? "Status"
                                : groupBy === "repository"
                                  ? "Repository"
                                  : "None"
                            }
                          />
                        }
                      >
                        Group by
                      </DropdownMenuTrigger>
                      <DropdownMenuContent>
                        <DropdownMenuRadioGroup
                          value={groupBy}
                          onValueChange={(value) => setGroupBy(String(value))}
                        >
                          <DropdownMenuRadioItem closeOnClick value="status">
                            Status
                          </DropdownMenuRadioItem>
                          <DropdownMenuRadioItem
                            closeOnClick
                            value="repository"
                          >
                            Repository
                          </DropdownMenuRadioItem>
                          <DropdownMenuRadioItem closeOnClick value="none">
                            No grouping
                          </DropdownMenuRadioItem>
                        </DropdownMenuRadioGroup>
                      </DropdownMenuContent>
                    </DropdownMenu>
                    <DropdownMenu>
                      <DropdownMenuTrigger
                        render={<ToolbarButton icon="sort" />}
                      >
                        Sort
                      </DropdownMenuTrigger>
                      <DropdownMenuContent>
                        <DropdownMenuRadioGroup
                          value={sort}
                          onValueChange={(value) => setSort(String(value))}
                        >
                          <DropdownMenuRadioItem closeOnClick value="updated">
                            Recently updated
                          </DropdownMenuRadioItem>
                          <DropdownMenuRadioItem closeOnClick value="oldest">
                            Oldest updated
                          </DropdownMenuRadioItem>
                          <DropdownMenuRadioItem closeOnClick value="name">
                            Name A–Z
                          </DropdownMenuRadioItem>
                        </DropdownMenuRadioGroup>
                      </DropdownMenuContent>
                    </DropdownMenu>
                    <DropdownMenu>
                      <DropdownMenuTrigger
                        render={<ToolbarButton icon="settings" />}
                      >
                        View
                      </DropdownMenuTrigger>
                      <DropdownMenuContent>
                        <DropdownMenuGroup>
                          <DropdownMenuCheckboxItem
                            checked={compact}
                            onCheckedChange={setCompact}
                          >
                            Compact rows
                          </DropdownMenuCheckboxItem>
                          <DropdownMenuCheckboxItem
                            checked={hideEmpty}
                            onCheckedChange={setHideEmpty}
                          >
                            Hide empty groups
                          </DropdownMenuCheckboxItem>
                        </DropdownMenuGroup>
                      </DropdownMenuContent>
                    </DropdownMenu>
                    <DropdownMenu>
                      <DropdownMenuTrigger
                        render={
                          <ToolbarButton icon="filter" active={filtered} />
                        }
                      >
                        Filter
                      </DropdownMenuTrigger>
                      <DropdownMenuContent className="min-w-52">
                        <DropdownMenuGroup>
                          <DropdownMenuLabel>Repository</DropdownMenuLabel>
                          <DropdownMenuRadioGroup
                            value={repo}
                            onValueChange={(value) => setRepo(String(value))}
                          >
                            <DropdownMenuRadioItem closeOnClick value="all">
                              All repositories
                            </DropdownMenuRadioItem>
                            {repositories.map((name) => (
                              <DropdownMenuRadioItem
                                closeOnClick
                                key={name}
                                value={name}
                              >
                                workspace/{name}
                              </DropdownMenuRadioItem>
                            ))}
                          </DropdownMenuRadioGroup>
                        </DropdownMenuGroup>
                        <DropdownMenuSeparator />
                        <DropdownMenuGroup>
                          <DropdownMenuItem onClick={resetFilters}>
                            Clear filters
                          </DropdownMenuItem>
                        </DropdownMenuGroup>
                      </DropdownMenuContent>
                    </DropdownMenu>
                  </>
                )}
                <Tooltip>
                  <TooltipTrigger
                    render={
                      <Button
                        variant="outline"
                        size="icon"
                        aria-label="Refresh tasks"
                        onClick={() =>
                          toast.add({ title: "Tasks refreshed.", type: "info" })
                        }
                      />
                    }
                  >
                    <Icon icon="refresh" size={15} />
                  </TooltipTrigger>
                  <TooltipContent>Refresh tasks</TooltipContent>
                </Tooltip>
              </ToolbarFilters>
            </ViewToolbar>
            <PageContent>
              <PreviewBanner
                label="Sample workspace"
                explanation="Explore the layout with example tasks"
                action="Back to live tasks"
              />
              {filtered && (view === "list" || view === "kanban") && (
                <ActiveFilters onClear={resetFilters}>
                  {visible.length} matching{" "}
                  {visible.length === 1 ? "task" : "tasks"}
                  {repo !== "all" ? ` · workspace/${repo}` : ""}
                </ActiveFilters>
              )}
              {view === "activity" ? (
                <WorkspaceView>
                  <ViewHeading
                    icon="activity"
                    title="Workspace activity"
                    description="The latest events across your coding tasks."
                  />
                  <EventList>
                    {events.map((event) => (
                      <EventItem
                        key={`${event.type}-${event.time}`}
                        type={event.type.replaceAll("_", " ")}
                        time={event.time}
                        error={event.error}
                        reference={event.reference}
                      >
                        {event.message}
                      </EventItem>
                    ))}
                  </EventList>
                </WorkspaceView>
              ) : view === "dashboard" ? (
                <WorkspaceView>
                  <ViewHeading
                    icon="dashboard"
                    title="Queue overview"
                    description="Worker availability and task processing, updated automatically."
                  />
                  <StatusHeading
                    title="application-tasks"
                    status="Accepting tasks"
                  />
                  <StatGrid>
                    <Stat label="waiting" value={3} />
                    <Stat label="active" value={7} />
                    <Stat label="completed" value={128} />
                    <Stat label="failed" value={4} />
                    <Stat label="delayed" value={0} />
                    <Stat label="paused" value={0} />
                  </StatGrid>
                  <Footnotes>
                    <Footnote icon="robot">2 connected workers</Footnote>
                    <Footnote icon="clock">
                      Oldest waiting task: 12 seconds
                    </Footnote>
                    <Footnote>Updated Sep 8, 9:06 AM</Footnote>
                  </Footnotes>
                </WorkspaceView>
              ) : visible.length === 0 ? (
                <EmptyWorkspace
                  illustration={<EmptyIllustration name="search" />}
                  eyebrow="Your coding workspace"
                  title="No tasks match your filters"
                  description="Try another search or clear your filters to see more tasks."
                  actions={
                    <Button variant="outline" onClick={resetFilters}>
                      Clear filters
                    </Button>
                  }
                  steps={[
                    { icon: "task", label: "Describe" },
                    { icon: "code", label: "Implement" },
                    { icon: "review", label: "Review" },
                    { icon: "completed", label: "Done" },
                  ]}
                />
              ) : view === "kanban" ? (
                <TaskBoard
                  columns={shownSections.map((section) => ({
                    id: section.id,
                    title: section.title,
                    symbol: section.symbol,
                    tasks: section.tasks,
                  }))}
                  onSelect={setSelected}
                  actions={(column) => (
                    <>
                      <TaskGroupAction
                        icon="more"
                        aria-label={`${column.title} options`}
                      />
                      <TaskGroupAction
                        icon="addCircle"
                        aria-label="Create coding task"
                        onClick={() => setCreating(true)}
                      />
                    </>
                  )}
                />
              ) : (
                <TaskList density={compact ? "compact" : "default"}>
                  {shownSections.map((section) => (
                    <TaskGroup
                      key={section.id}
                      title={section.title}
                      count={section.tasks.length}
                      symbol={section.symbol}
                      actions={
                        <>
                          <TaskGroupAction
                            icon="more"
                            aria-label={`${section.title} options`}
                          />
                          <TaskGroupAction
                            icon="addCircle"
                            aria-label="Create coding task"
                            onClick={() => setCreating(true)}
                          />
                        </>
                      }
                    >
                      <TaskTable tasks={section.tasks} onSelect={setSelected} />
                    </TaskGroup>
                  ))}
                </TaskList>
              )}
            </PageContent>
            <WorkspaceFooter>
              <span>
                <ConnectionDot online={false} />
                Read-only preview
              </span>
              <span>{visible.length} tasks</span>
              <FooterShortcut keys="N">New task</FooterShortcut>
            </WorkspaceFooter>
          </>
        )}
      </MainPanel>
      <CreateTaskSheet open={creating} onOpenChange={setCreating} />
    </AppShell>
  )
}
