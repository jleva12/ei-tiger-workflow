import * as React from "react"

import { Button } from "@/components/ui/button"
import { Sheet, SheetTrigger } from "@/components/ui/sheet"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
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
  MainPanel,
  PageContent,
  Topbar,
  TopbarBreadcrumb,
  TopbarCrumb,
  TopbarCrumbSeparator,
  TopbarPage,
} from "@/components/forge/app-shell"
import { WorkspaceOrb } from "@/components/forge/avatars"
import {
  EmptyIllustration,
  EmptyWorkspace,
  PageEmpty,
  PanelEmpty,
} from "@/components/forge/empty-state"
import { ErrorCallout } from "@/components/forge/feedback"
import { Icon } from "@/components/forge/icon"
import {
  AppMark,
  IconRail,
  RailButton,
  RailFooter,
  RailNav,
} from "@/components/forge/icon-rail"
import {
  Illustration,
  type IllustrationName,
} from "@/components/forge/illustration"
import { BoardCard, KanbanBoard, KanbanColumn } from "@/components/forge/kanban"
import { LogEmpty } from "@/components/forge/run-log"
import { ConnectionDot } from "@/components/forge/status"
import {
  TaskGroup,
  TaskGroupAction,
  TaskList,
  TaskListSkeleton,
  TaskTable,
} from "@/components/forge/task-list"
import {
  PlanAgent,
  PlanEntry,
  TaskSheetContent,
  TaskSheetHeader,
} from "@/components/forge/task-sheet"
import {
  CommandButton,
  NavItem,
  NavSectionHeading,
  ProjectItem,
  SidebarBrand,
  SidebarBrandName,
  SidebarCollapseButton,
  SidebarHint,
  SidebarSection,
  SidebarStatus,
  WorkspaceSidebar,
} from "@/components/forge/workspace-sidebar"
import { CreateTaskSheet } from "../create-task-sheet"
import { events, groupTasks, kanbanColumns, tasks } from "../data"
import { SectionPage, Specimen } from "../specimen"
import { WorkspacePreview } from "../workspace-preview"

/* -------------------------------------------------------------------------- */
/* App shell                                                                  */
/* -------------------------------------------------------------------------- */

const frames = [
  { id: "full", label: "Full", width: "100%" },
  { id: "laptop", label: "1180", width: "1180px" },
  { id: "tablet", label: "900", width: "900px" },
  { id: "phone", label: "390", width: "390px" },
] as const

export function ShellSection() {
  const [frame, setFrame] = React.useState<string>("full")
  const width = frames.find((item) => item.id === frame)?.width ?? "100%"
  return (
    <SectionPage
      icon="dashboard"
      eyebrow="Workspace"
      title="App shell"
      description="A 56px icon rail, a 224px workspace sidebar and a fluid main panel with a 58px header, a view toolbar and a status footer. The shell is a container: it adapts to its own width (1700 / 1270 / 1050 / 800 / 600px), so resize the frame to see the sidebar collapse into a sheet and the toolbar wrap."
    >
      <Specimen
        title="Forge Workspace, rebuilt from the library"
        description="Fully interactive: switch views, search, group, filter by project, collapse groups, open a task's detail page, or create a task."
        variant="canvas"
        className="p-0 @max-[600px]/shell:p-0"
        code={`<AppShell>
  <IconRail>
    <AppMark />
    <RailNav>
      <RailButton icon="home" label="Task overview" active />
      …
    </RailNav>
    <RailFooter>…</RailFooter>
  </IconRail>
  <WorkspaceSidebar>
    <SidebarBrand>
      <WorkspaceOrb />
      <SidebarBrandName name="Forge" suffix="Workspace" />
      <SidebarCollapseButton />
    </SidebarBrand>
    <SidebarSection variant="primary">
      <CommandButton />
      <NavItem icon="home">Home</NavItem>
    </SidebarSection>
    <SidebarStatus>All changes synced</SidebarStatus>
  </WorkspaceSidebar>
  <MainPanel>
    <Topbar>
      <TopbarBreadcrumb>…</TopbarBreadcrumb>
      <TopbarActions>
        <SearchField />
        <PrimaryAction>Task</PrimaryAction>
      </TopbarActions>
    </Topbar>
    <ViewToolbar>…</ViewToolbar>
    <PageContent>…</PageContent>
    <WorkspaceFooter>…</WorkspaceFooter>
  </MainPanel>
</AppShell>`}
      >
        <div className="flex items-center justify-between gap-3 border-b bg-background px-4 py-2.5">
          <span className="flex items-center gap-2 text-2xs text-muted-foreground">
            <Icon icon="view" size={14} />
            Frame width
          </span>
          <Button
            variant="ghost"
            size="xs"
            className="ml-auto text-muted-foreground"
            nativeButton={false}
            render={<a href="#workspace" />}
          >
            Open full screen
            <Icon icon="external" data-icon="inline-end" />
          </Button>
          <ToggleGroup
            value={[frame]}
            onValueChange={(value) => value[0] && setFrame(String(value[0]))}
            variant="outline"
            aria-label="Frame width"
          >
            {frames.map((item) => (
              <ToggleGroupItem
                key={item.id}
                value={item.id}
                size="sm"
                className="text-xs"
              >
                {item.label}
              </ToggleGroupItem>
            ))}
          </ToggleGroup>
        </div>
        <div className="overflow-x-auto p-4">
          <div
            className="mx-auto overflow-hidden rounded-(--radius-band) border bg-background shadow-(--shadow-float) transition-[width] duration-300"
            style={{ width, maxWidth: "100%" }}
          >
            <WorkspacePreview className="h-[780px]" />
          </div>
        </div>
      </Specimen>
    </SectionPage>
  )
}

/* -------------------------------------------------------------------------- */
/* Navigation                                                                 */
/* -------------------------------------------------------------------------- */

export function NavigationSection() {
  const [active, setActive] = React.useState("tasks")
  return (
    <SectionPage
      icon="sidebar"
      eyebrow="Workspace"
      title="Navigation"
      description="The icon rail holds global shortcuts with right-side tooltips; the workspace sidebar holds the command entry, primary destinations, grouped sections, project markers and a sync status. Rows are 36px with a muted fill for hover and the current page."
    >
      <Specimen
        title="Rail + sidebar"
        variant="flush"
        code={`<NavItem icon="task" active meta={15}>Tasks</NavItem>
<NavItem icon="bell" meta={<ConnectionDot />}>Updates</NavItem>
<NavSectionHeading action={<Button variant="ghost" size="icon-xs">…</Button>}>
  Projects
</NavSectionHeading>
<ProjectItem color={1}>admin-api</ProjectItem>
<SidebarStatus online>All changes synced</SidebarStatus>`}
      >
        <AppShell className="h-[640px] min-w-0">
          <IconRail>
            <AppMark aria-label="Home" />
            <RailNav aria-label="Shortcuts">
              <RailButton icon="home" label="Task overview" active />
              <RailButton icon="search" label="Search tasks" />
              <RailButton icon="bell" label="Activity" />
              <RailButton icon="folder" label="Projects" />
              <RailButton icon="task" label="New task" />
            </RailNav>
            <RailFooter>
              <RailButton icon="sun" label="Toggle color theme" />
              <RailButton icon="settings" label="Settings" />
              <WorkspaceOrb size="lg" className="mt-[7px]" />
            </RailFooter>
          </IconRail>
          <WorkspaceSidebar>
            <SidebarBrand>
              <WorkspaceOrb />
              <SidebarBrandName name="Forge" suffix="Workspace" />
              <SidebarCollapseButton />
            </SidebarBrand>
            <SidebarSection variant="primary">
              <CommandButton />
              <NavItem
                icon="home"
                active={active === "home"}
                onClick={() => setActive("home")}
              >
                Home
              </NavItem>
              <NavItem
                icon="bell"
                active={active === "updates"}
                onClick={() => setActive("updates")}
                meta={<ConnectionDot />}
              >
                Updates
              </NavItem>
              <NavItem
                icon="activity"
                active={active === "active"}
                onClick={() => setActive("active")}
                meta={7}
              >
                Active tasks
              </NavItem>
            </SidebarSection>
            <SidebarSection>
              <NavSectionHeading
                action={
                  <Button
                    variant="ghost"
                    size="icon-xs"
                    aria-label="Create task"
                  >
                    <Icon icon="plus" size={14} />
                  </Button>
                }
              >
                Workspace
              </NavSectionHeading>
              <NavItem
                icon="task"
                active={active === "tasks"}
                onClick={() => setActive("tasks")}
                meta={15}
              >
                Tasks
              </NavItem>
              <NavItem
                icon="team"
                active={active === "plans"}
                onClick={() => setActive("plans")}
              >
                Agent plans
              </NavItem>
            </SidebarSection>
            <SidebarSection variant="flush">
              <NavSectionHeading>Projects</NavSectionHeading>
              {["web", "admin-api", "coding-agent", "common"].map(
                (name, index) => (
                  <ProjectItem
                    key={name}
                    color={index}
                    active={active === name}
                    onClick={() => setActive(name)}
                  >
                    {name}
                  </ProjectItem>
                )
              )}
              <SidebarHint>
                Repositories appear here
                <br />
                when you create a task.
              </SidebarHint>
            </SidebarSection>
            <SidebarStatus>All changes synced</SidebarStatus>
          </WorkspaceSidebar>
          <MainPanel>
            <Topbar>
              <TopbarBreadcrumb>
                <TopbarCrumb icon="task">Tasks</TopbarCrumb>
                <TopbarCrumbSeparator />
                <TopbarPage icon="dashboard">Coding Tasks</TopbarPage>
              </TopbarBreadcrumb>
            </Topbar>
            <PageContent>
              <p className="text-xs text-muted-foreground">
                Hover the rail for tooltips. Collapse the sidebar with the
                button beside the brand; restore it from the header.
              </p>
            </PageContent>
          </MainPanel>
        </AppShell>
      </Specimen>
    </SectionPage>
  )
}

/* -------------------------------------------------------------------------- */
/* Task list                                                                  */
/* -------------------------------------------------------------------------- */

export function TaskListSection() {
  const [compact, setCompact] = React.useState(false)
  return (
    <SectionPage
      icon="list"
      eyebrow="Workspace"
      title="Task list"
      description="Status groups are full-width rounded pastel bands followed by flat table rows. Rows show a hairline outline on hover; the ID and title cell is the row's action. Bands collapse, and a compact density tightens everything."
    >
      <Specimen
        title="Grouped task table"
        code={`<TaskList density="default">
  <TaskGroup title="In Progress" symbol="progress" count={2}
    actions={<TaskGroupAction icon="addCircle" aria-label="Create task" />}>
    <TaskTable tasks={tasks} onSelect={(task) => open(task.id)} />
  </TaskGroup>
</TaskList>`}
      >
        <div className="mb-4 flex justify-end">
          <Button
            variant="outline"
            size="sm"
            onClick={() => setCompact((v) => !v)}
          >
            <Icon icon="settings" data-icon="inline-start" />
            {compact ? "Comfortable rows" : "Compact rows"}
          </Button>
        </div>
        <TaskList density={compact ? "compact" : "default"}>
          {groupTasks(tasks).map((group) => (
            <TaskGroup
              key={group.id}
              title={group.title}
              count={group.tasks.length}
              symbol={group.symbol}
              actions={
                <>
                  <TaskGroupAction
                    icon="more"
                    aria-label={`${group.title} options`}
                  />
                  <TaskGroupAction
                    icon="addCircle"
                    aria-label="Create coding task"
                  />
                </>
              }
            >
              <TaskTable tasks={group.tasks.slice(0, 3)} />
            </TaskGroup>
          ))}
          <TaskGroup title="Failed" count={0} symbol="failed">
            <TaskTable tasks={[]} />
          </TaskGroup>
        </TaskList>
      </Specimen>
      <Specimen title="Loading">
        <TaskListSkeleton groups={2} rows={2} />
      </Specimen>
    </SectionPage>
  )
}

/* -------------------------------------------------------------------------- */
/* Kanban                                                                     */
/* -------------------------------------------------------------------------- */

export function KanbanSection() {
  return (
    <SectionPage
      icon="kanban"
      eyebrow="Workspace"
      title="Kanban board"
      description="The same status bands head 295px columns of cards. Cards show the ID and status, the title, repository, date and agents."
    >
      <Specimen
        title="Board"
        variant="canvas"
        code={`<TaskBoard columns={columns} onSelect={(task) => open(task.id)} />

// or compose it:
<KanbanBoard>
  <KanbanColumn title="Backlog" symbol="backlog" count={5}>
    <BoardCard task={task} />
  </KanbanColumn>
</KanbanBoard>`}
      >
        <div className="overflow-x-auto">
          <KanbanBoard>
            {kanbanColumns.map((column) => (
              <KanbanColumn
                key={column.id}
                title={column.title}
                count={column.tasks.length}
                symbol={column.symbol}
                actions={
                  <>
                    <TaskGroupAction
                      icon="more"
                      aria-label={`${column.title} options`}
                    />
                    <TaskGroupAction
                      icon="addCircle"
                      aria-label="Create coding task"
                    />
                  </>
                }
              >
                {column.tasks.slice(0, 3).map((task) => (
                  <BoardCard key={task.id} task={task} />
                ))}
              </KanbanColumn>
            ))}
            <KanbanColumn title="Failed" count={0} symbol="failed" />
          </KanbanBoard>
        </div>
      </Specimen>
    </SectionPage>
  )
}

/* -------------------------------------------------------------------------- */
/* Empty states                                                               */
/* -------------------------------------------------------------------------- */

const illustrations: {
  name: IllustrationName
  use: string
}[] = [
  { name: "tasks", use: "First run, nothing created yet" },
  { name: "complete", use: "All caught up, nothing to do" },
  { name: "waiting", use: "Queued, no run recorded yet" },
  { name: "error", use: "A run or load failed" },
  { name: "search", use: "No results for a search or filter" },
  { name: "offline", use: "The API or a worker is unreachable" },
  { name: "locked", use: "No access to this workspace" },
  { name: "activity", use: "No events on the timeline yet" },
  { name: "logs", use: "No log output yet" },
  { name: "changes", use: "No file changes in this run" },
  { name: "conversation", use: "An empty assistant thread" },
]

export function EmptySection() {
  return (
    <SectionPage
      icon="task"
      eyebrow="Workspace"
      title="Empty states"
      description="Built on shadcn Empty. Full-page states pair a paper illustration with a spaced eyebrow, a 23px title, one or two actions and a workflow hint; panels use a small illustration or a single icon and line; detail tabs get a centered title and explanation."
    >
      <Specimen
        title="Illustrations"
        variant="flush"
        description={
          <>
            One CSS drawing per state: the same two tilted sheets, with
            what&rsquo;s written on the front sheet and a single accent (a spark
            glyph or a round status badge) telling the states apart. They follow
            light and dark, shrink with <code>size="sm"</code> and recolour for
            the log console with <code>surface="console"</code>.
          </>
        }
        code={`<Illustration name="search" />
<Illustration name="offline" size="sm" />
<Illustration name="logs" surface="console" />

// In the empty states
<EmptyWorkspace illustration={<EmptyIllustration name="search" />} … />
<PanelEmpty illustration="activity">…</PanelEmpty>
<PageEmpty illustration="waiting" title="…" />
<LogEmpty illustration="logs" title="…" />`}
      >
        <div className="grid grid-cols-4 gap-px bg-border @max-[1050px]/shell:grid-cols-3 @max-[600px]/shell:grid-cols-2">
          {illustrations.map((item) => (
            <div
              key={item.name}
              className="flex flex-col items-center gap-3 bg-background px-3 pt-7 pb-5 text-center"
            >
              <Illustration name={item.name} />
              <div className="flex flex-col gap-0.5">
                <code className="text-xs font-medium text-foreground">
                  {item.name}
                </code>
                <span className="text-2xs text-subtle">{item.use}</span>
              </div>
            </div>
          ))}
          <div className="flex flex-col items-center justify-center gap-3 bg-console px-3 pt-7 pb-5 text-center">
            <Illustration name="logs" surface="console" />
            <div className="flex flex-col gap-0.5">
              <code className="text-xs font-medium text-console-foreground">
                surface="console"
              </code>
              <span className="text-2xs text-console-muted">
                Inside the run log console
              </span>
            </div>
          </div>
        </div>
      </Specimen>
      <Specimen
        title="Workspace"
        code={`<EmptyWorkspace
  eyebrow="Your coding workspace"
  title="A clear space for your next idea"
  description="From a small fix to your next big feature…"
  actions={<>
    <Button><Icon icon="plus" data-icon="inline-start" />Create your first task</Button>
    <Button variant="ghost">Explore an example <Icon icon="external" data-icon="inline-end" /></Button>
  </>}
  steps={[{ icon: "task", label: "Describe" }, { icon: "code", label: "Implement" }, …]}
/>`}
      >
        <EmptyWorkspace
          className="min-h-[440px]"
          eyebrow="Your coding workspace"
          title="A clear space for your next idea"
          description="From a small fix to your next big feature. Create a task and let your coding agents take it from here."
          actions={
            <>
              <Button>
                <Icon icon="plus" data-icon="inline-start" />
                Create your first task
              </Button>
              <Button variant="ghost">
                Explore an example
                <Icon icon="external" data-icon="inline-end" />
              </Button>
            </>
          }
          steps={[
            { icon: "task", label: "Describe" },
            { icon: "code", label: "Implement" },
            { icon: "review", label: "Review" },
            { icon: "completed", label: "Done" },
          ]}
        />
      </Specimen>
      <div className="grid grid-cols-2 gap-6 @max-[1050px]/shell:grid-cols-1">
        <Specimen
          title="No results"
          code={`<EmptyWorkspace
  illustration={<EmptyIllustration name="search" />}
  title="No tasks match your filters"
  actions={<Button variant="outline">Clear filters</Button>}
/>`}
        >
          <EmptyWorkspace
            className="min-h-0 pb-6"
            illustration={<EmptyIllustration name="search" />}
            title="No tasks match your filters"
            description="Try another search or clear your filters to see more tasks."
            actions={<Button variant="outline">Clear filters</Button>}
          />
        </Specimen>
        <Specimen title="Connection problem">
          <EmptyWorkspace
            className="min-h-0 pb-6"
            illustration={<EmptyIllustration name="offline" />}
            title="Let’s get you connected"
            description="Your task workspace is ready. Reconnect the admin API to load your tasks."
            actions={<Button variant="outline">Retry</Button>}
          />
        </Specimen>
        <Specimen title="Something went wrong">
          <EmptyWorkspace
            className="min-h-0 pb-6"
            illustration={<EmptyIllustration name="error" />}
            title="We couldn’t load your tasks"
            description="The admin API returned 502 Bad Gateway. Your tasks are safe — try again in a moment."
            actions={
              <Button variant="outline">
                <Icon icon="refresh" data-icon="inline-start" />
                Try again
              </Button>
            }
          />
        </Specimen>
        <Specimen title="No access">
          <EmptyWorkspace
            className="min-h-0 pb-6"
            illustration={<EmptyIllustration name="locked" />}
            title="This workspace is private"
            description="Ask a workspace admin to invite you, then reload this page."
            actions={<Button variant="outline">Request access</Button>}
          />
        </Specimen>
      </div>
      <div className="grid grid-cols-2 gap-6 @max-[1050px]/shell:grid-cols-1">
        <Specimen
          title="Panel"
          code={`<PanelEmpty illustration="activity">
  Your task activity will appear here as work gets underway.
</PanelEmpty>`}
        >
          <PanelEmpty illustration="activity">
            Your task activity will appear here as work gets underway.
          </PanelEmpty>
        </Specimen>
        <Specimen title="Panel, all done">
          <PanelEmpty illustration="complete">
            Nothing waiting for review. Nice work.
          </PanelEmpty>
        </Specimen>
        <Specimen title="Panel with an icon">
          <PanelEmpty icon="activity">
            Your task activity will appear here as work gets underway.
          </PanelEmpty>
        </Specimen>
        <Specimen title="Log console" variant="flush">
          <div className="bg-console">
            <LogEmpty illustration="logs" title="Waiting for output">
              Logs stream here as soon as a worker starts the run.
            </LogEmpty>
          </div>
        </Specimen>
      </div>
      <div className="grid grid-cols-2 gap-6 @max-[1050px]/shell:grid-cols-1">
        <Specimen
          title="Detail tab"
          code={`<PageEmpty
  illustration="waiting"
  title="No run has been recorded yet"
  description="The task is registered…"
/>`}
        >
          <PageEmpty
            illustration="waiting"
            title="No run has been recorded yet"
            description="The task is registered. Its submission will appear here when a run is created."
          />
        </Specimen>
        <Specimen title="Detail tab, no changes">
          <PageEmpty
            illustration="changes"
            title="No file changes yet"
            description="The diff appears here once the implementer commits to the task branch."
          />
        </Specimen>
      </div>
    </SectionPage>
  )
}

/* -------------------------------------------------------------------------- */
/* Activity & dashboard                                                       */
/* -------------------------------------------------------------------------- */

export function ActivitySection() {
  return (
    <SectionPage
      icon="activity"
      eyebrow="Workspace"
      title="Activity & dashboard"
      description="Secondary views use a readable 920px column: an event timeline with status dots, and a hairline grid of large queue statistics."
    >
      <div className="grid grid-cols-2 gap-6 @max-[1050px]/shell:grid-cols-1">
        <Specimen
          title="Event list"
          code={`<EventList>
  <EventItem type="run started" time="Sep 8, 9:02 AM" reference="FG-214">
    Implementer picked up the task.
  </EventItem>
  <EventItem type="verification failed" error …>…</EventItem>
</EventList>`}
        >
          <WorkspaceView className="p-0">
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
        </Specimen>
        <Specimen
          title="Queue overview"
          code={`<StatusHeading title="application-tasks" status="Accepting tasks" />
<StatGrid>
  <Stat label="waiting" value={3} />
  <Stat label="active" value={7} />
</StatGrid>
<Footnotes><Footnote icon="robot">2 connected workers</Footnote></Footnotes>`}
        >
          <WorkspaceView className="p-0">
            <ViewHeading
              icon="dashboard"
              title="Queue overview"
              description="Worker availability and task processing, updated automatically."
            />
            <StatusHeading title="application-tasks" status="Accepting tasks" />
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
              <Footnote icon="clock">Oldest waiting task: 12 seconds</Footnote>
            </Footnotes>
          </WorkspaceView>
        </Specimen>
      </div>
      <Specimen title="Connection error">
        <ErrorCallout
          title="Couldn’t load activity"
          action={<Button variant="outline">Retry</Button>}
        >
          Request to /admin/events/recent failed with 503 Service Unavailable.
        </ErrorCallout>
      </Specimen>
    </SectionPage>
  )
}

/* -------------------------------------------------------------------------- */
/* Sheets & forms                                                             */
/* -------------------------------------------------------------------------- */

export function SheetsSection() {
  const [creating, setCreating] = React.useState(false)
  return (
    <SectionPage
      icon="robot"
      eyebrow="Workspace"
      title="Sheets & forms"
      description="Creation stays in an accessible 560px side sheet: an eyebrow, a 23px title, a compact form at 12px with 36px fields, and a footer hint beside the submit action. Validation errors are inline and recoverable."
    >
      <Specimen
        title="Create a task"
        code={`<Sheet open={open} onOpenChange={setOpen}>
  <TaskSheetContent>
    <TaskSheetHeader eyebrow="Coding workspace" title="Create a task"
      description="Describe the change…" />
    <TaskForm onSubmit={submit}>
      <FieldGroup>
        <Field>
          <FieldLabel htmlFor="description">What needs to be done?</FieldLabel>
          <Textarea id="description" />
          <FieldDescription>Include the expected behavior…</FieldDescription>
        </Field>
        <FormColumns>…</FormColumns>
      </FieldGroup>
      <FormFooter hint="Runs on a dedicated task branch">
        <Button type="submit">Create task</Button>
      </FormFooter>
    </TaskForm>
  </TaskSheetContent>
</Sheet>`}
      >
        <div className="flex flex-wrap items-center gap-3">
          <Button onClick={() => setCreating(true)}>
            <Icon icon="plus" data-icon="inline-start" />
            Create a task
          </Button>
          <span className="text-2xs text-muted-foreground">
            Submit with fewer than 8 characters to see inline validation.
          </span>
        </div>
        <CreateTaskSheet open={creating} onOpenChange={setCreating} />
      </Specimen>
      <Specimen title="Plans list sheet">
        <Sheet>
          <SheetTrigger render={<Button variant="outline" />}>
            <Icon icon="team" data-icon="inline-start" />
            View agent plans
          </SheetTrigger>
          <TaskSheetContent>
            <TaskSheetHeader
              eyebrow="Coding workspace"
              eyebrowIcon="robot"
              title="Agent plans"
              description="Reusable agent teams available for new coding tasks."
            />
            <div className="px-7 pb-7">
              <PlanEntry
                title="Default coding workflow"
                meta="Revision 4"
                description="An implementer writes the change, a reviewer checks it against the brief, and a fixer resolves blocking findings."
              >
                <PlanAgent
                  name="Implementer"
                  detail="implementer · preview-model"
                />
                <PlanAgent
                  name="Code reviewer"
                  detail="reviewer · preview-model"
                />
                <PlanAgent name="Fixer" detail="fixer · preview-model" />
              </PlanEntry>
              <PlanEntry
                title="Review-heavy workflow"
                meta="Revision 2"
                description="Adds a security reviewer in parallel with the code reviewer."
              >
                <PlanAgent
                  name="Implementer"
                  detail="implementer · preview-model"
                />
                <PlanAgent
                  name="Security reviewer"
                  detail="reviewer · preview-model"
                />
              </PlanEntry>
            </div>
          </TaskSheetContent>
        </Sheet>
      </Specimen>
    </SectionPage>
  )
}
