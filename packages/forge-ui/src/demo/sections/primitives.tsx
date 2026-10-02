import * as React from "react"

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import {
  Command,
  CommandDialog,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
  CommandSeparator,
  CommandShortcut,
} from "@/components/ui/command"
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog"
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
  DropdownMenuShortcut,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import {
  Field,
  FieldDescription,
  FieldError,
  FieldGroup,
  FieldLabel,
} from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { Kbd } from "@/components/ui/kbd"
import { Progress } from "@/components/ui/progress"
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Sheet, SheetTrigger } from "@/components/ui/sheet"
import { Spinner } from "@/components/ui/spinner"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { Textarea } from "@/components/ui/textarea"
import { toast } from "@/components/ui/toast"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { PrimaryAction } from "@/components/forge/app-shell"
import { AgentAvatars, AgentOrb, WorkspaceOrb } from "@/components/forge/avatars"
import { FileFilter } from "@/components/forge/changes"
import {
  ActiveFilters,
  ErrorCallout,
  LoadMore,
  PreviewBanner,
} from "@/components/forge/feedback"
import { VerificationStatus } from "@/components/forge/handoff"
import { Icon } from "@/components/forge/icon"
import {
  Chip,
  ConnectionDot,
  CountBadge,
  RunStateIcon,
  StatusBadge,
  StatusSymbol,
} from "@/components/forge/status"
import { TaskListSkeleton } from "@/components/forge/task-list"
import {
  DetailList,
  DetailRow,
  TaskSheetContent,
  TaskSheetHeader,
} from "@/components/forge/task-sheet"
import { TaskTabsList, TaskTabsTrigger } from "@/components/forge/task-page"
import {
  LayoutSwitch,
  SearchField,
  ToolbarButton,
  ViewTabsList,
  ViewTabsTrigger,
} from "@/components/forge/toolbar"
import { CommandButton } from "@/components/forge/workspace-sidebar"
import { agents } from "../data"
import { Row, SectionPage, Specimen } from "../specimen"

/* -------------------------------------------------------------------------- */
/* Buttons                                                                    */
/* -------------------------------------------------------------------------- */

export function ButtonsSection() {
  const [saving, setSaving] = React.useState(false)
  return (
    <SectionPage
      icon="plus"
      eyebrow="Primitives"
      title="Buttons"
      description="shadcn Button tuned to the workspace: 7px radius, 12px/450 labels, colour-only 150ms transitions and a barely-there shadow on outline buttons. One dark primary action per view; everything else is outline or ghost."
    >
      <Specimen
        title="Variants"
        code={`<Button>Create task</Button>
<Button variant="outline">Retry</Button>
<Button variant="ghost">Explore an example</Button>`}
      >
        <Row>
          <Button>Primary</Button>
          <Button variant="outline">Outline</Button>
          <Button variant="secondary">Secondary</Button>
          <Button variant="ghost">Ghost</Button>
          <Button variant="destructive">Destructive</Button>
          <Button variant="link">Link</Button>
        </Row>
      </Specimen>
      <Specimen title="Sizes">
        <Row>
          <Button size="xs">Extra small</Button>
          <Button size="sm">Small</Button>
          <Button>Default</Button>
          <Button size="lg">Large</Button>
          <span className="mx-2 h-6 w-px bg-border" />
          <Button size="icon-xs" variant="outline" aria-label="Add">
            <Icon icon="plus" />
          </Button>
          <Button size="icon-sm" variant="outline" aria-label="Add">
            <Icon icon="plus" />
          </Button>
          <Button size="icon" variant="outline" aria-label="Refresh">
            <Icon icon="refresh" />
          </Button>
          <Button size="icon-lg" variant="outline" aria-label="Settings">
            <Icon icon="settings" />
          </Button>
        </Row>
      </Specimen>
      <Specimen
        title="With icons & pending state"
        description="Icons carry data-icon so the button can balance its padding. Pending actions compose Spinner + disabled."
        code={`<Button variant="outline" size="sm">
  <Icon icon="link" data-icon="inline-start" />
  Copy link
</Button>
<Button disabled>
  <Spinner data-icon="inline-start" />
  Creating…
</Button>`}
      >
        <Row>
          <Button variant="outline" size="sm">
            <Icon icon="link" data-icon="inline-start" />
            Copy link
          </Button>
          <Button variant="outline" size="sm">
            <Icon icon="review" data-icon="inline-start" />
            Open PR
          </Button>
          <Button variant="outline" size="sm">
            <Icon icon="stop" data-icon="inline-start" />
            Cancel run
          </Button>
          <Button variant="outline" size="sm">
            <Icon icon="download" data-icon="inline-start" />
            Download logs
          </Button>
          <Button variant="ghost">
            Explore an example
            <Icon icon="external" data-icon="inline-end" />
          </Button>
          <Button
            disabled={saving}
            onClick={() => {
              setSaving(true)
              setTimeout(() => setSaving(false), 1600)
            }}
          >
            {saving ? (
              <Spinner data-icon="inline-start" />
            ) : (
              <Icon icon="plus" data-icon="inline-start" />
            )}
            {saving ? "Creating…" : "Create task"}
          </Button>
        </Row>
      </Specimen>
      <Specimen
        title="Workspace controls"
        description="Composites built on Button: the command palette entry, toolbar buttons with an emphasised value or an active-filter dot, and the header's primary action."
        code={`<CommandButton shortcut="⌘ K" />
<ToolbarButton icon="dashboard" value="Status">Group by</ToolbarButton>
<ToolbarButton icon="filter" active>Filter</ToolbarButton>
<PrimaryAction>Task</PrimaryAction>`}
      >
        <div className="flex flex-col gap-5">
          <div className="w-[194px]">
            <CommandButton className="mb-0" />
          </div>
          <Row className="gap-[7px]">
            <ToolbarButton icon="dashboard" value="Status">
              Group by
            </ToolbarButton>
            <ToolbarButton icon="sort">Sort</ToolbarButton>
            <ToolbarButton icon="settings">View</ToolbarButton>
            <ToolbarButton icon="filter">Filter</ToolbarButton>
            <ToolbarButton icon="filter" active>
              Filter
            </ToolbarButton>
            <Button variant="outline" size="icon" aria-label="Refresh">
              <Icon icon="refresh" size={15} />
            </Button>
            <PrimaryAction>Task</PrimaryAction>
          </Row>
        </div>
      </Specimen>
    </SectionPage>
  )
}

/* -------------------------------------------------------------------------- */
/* Inputs & forms                                                             */
/* -------------------------------------------------------------------------- */

const demoPlans = [
  { value: "default", label: "Default coding workflow" },
  { value: "review", label: "Review-heavy · 4 agents" },
]

export function InputsSection() {
  return (
    <SectionPage
      icon="file"
      eyebrow="Primitives"
      title="Inputs & forms"
      description="Bordered fields on the page background with a 7px radius. Forms compose shadcn Field / FieldGroup; validation uses data-invalid on the field and aria-invalid on the control."
    >
      <Specimen
        title="Search"
        description="Header search with a leading glyph and a keyboard hint, and the file filter used in the Changes tab."
        code={`<SearchField placeholder="Search" shortcut="/" />
<FileFilter />`}
      >
        <Row className="gap-6">
          <SearchField />
          <SearchField
            className="w-72"
            placeholder="Search tasks"
            shortcut="⌘ K"
          />
          <FileFilter className="w-64" />
        </Row>
      </Specimen>
      <Specimen
        title="Fields"
        code={`<FieldGroup>
  <Field>
    <FieldLabel htmlFor="repository">Repository</FieldLabel>
    <Input id="repository" placeholder="https://github.com/…" />
    <FieldDescription>Runs on a dedicated task branch.</FieldDescription>
  </Field>
  <Field>
    <FieldLabel htmlFor="plan">Agent plan</FieldLabel>
    <Select items={plans} defaultValue="default">
      <SelectTrigger id="plan" className="w-full">
        <SelectValue />
      </SelectTrigger>
      <SelectContent alignItemWithTrigger={false}>
        {plans.map((plan) => (
          <SelectItem key={plan.value} value={plan.value}>
            {plan.label}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  </Field>
  <Field data-invalid>
    <FieldLabel htmlFor="description">What needs to be done?</FieldLabel>
    <Textarea id="description" aria-invalid />
    <FieldError>Describe the task in at least 8 characters.</FieldError>
  </Field>
</FieldGroup>`}
      >
        <FieldGroup className="max-w-xl">
          <Field>
            <FieldLabel htmlFor="demo-repository">Repository</FieldLabel>
            <Input
              id="demo-repository"
              placeholder="https://github.com/your-team/repository.git"
            />
            <FieldDescription>
              The agent clones this repository into an isolated workspace.
            </FieldDescription>
          </Field>
          <div className="grid grid-cols-2 gap-5 @max-[600px]/shell:grid-cols-1">
            <Field>
              <FieldLabel htmlFor="demo-plan">Agent plan</FieldLabel>
              <Select items={demoPlans} defaultValue="default">
                <SelectTrigger id="demo-plan" className="w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent alignItemWithTrigger={false}>
                  <SelectGroup>
                    {demoPlans.map((plan) => (
                      <SelectItem key={plan.value} value={plan.value}>
                        {plan.label}
                      </SelectItem>
                    ))}
                  </SelectGroup>
                </SelectContent>
              </Select>
            </Field>
            <Field data-disabled>
              <FieldLabel htmlFor="demo-base">Base branch</FieldLabel>
              <Input id="demo-base" defaultValue="main" disabled />
            </Field>
          </div>
          <Field data-invalid>
            <FieldLabel htmlFor="demo-description">
              What needs to be done?
            </FieldLabel>
            <Textarea
              id="demo-description"
              aria-invalid
              defaultValue="Fix"
              rows={3}
            />
            <FieldError>Describe the task in at least 8 characters.</FieldError>
          </Field>
        </FieldGroup>
      </Specimen>
    </SectionPage>
  )
}

/* -------------------------------------------------------------------------- */
/* Menus & overlays                                                           */
/* -------------------------------------------------------------------------- */

/** The palette's contents, shared by the inline and dialog specimens. */
function DemoCommands({ onRun }: { onRun: (label: string) => void }) {
  return (
    <>
      <CommandInput placeholder="Search commands…" />
      <CommandList>
        <CommandEmpty>No commands found.</CommandEmpty>
        <CommandGroup heading="Tasks">
          <CommandItem onSelect={() => onRun("New task")}>
            <Icon icon="plus" />
            New task
            <CommandShortcut>N</CommandShortcut>
          </CommandItem>
          <CommandItem onSelect={() => onRun("Search tasks")}>
            <Icon icon="search" />
            Search tasks
            <CommandShortcut>/</CommandShortcut>
          </CommandItem>
          <CommandItem onSelect={() => onRun("Refresh tasks")}>
            <Icon icon="refresh" />
            Refresh tasks
            <CommandShortcut>R</CommandShortcut>
          </CommandItem>
        </CommandGroup>
        <CommandSeparator />
        <CommandGroup heading="Go to">
          <CommandItem onSelect={() => onRun("Overview")}>
            <Icon icon="home" />
            Overview
          </CommandItem>
          <CommandItem onSelect={() => onRun("Activity")}>
            <Icon icon="activity" />
            Activity
          </CommandItem>
          <CommandItem disabled>
            <Icon icon="settings" />
            Settings
          </CommandItem>
        </CommandGroup>
      </CommandList>
    </>
  )
}

export function MenusSection() {
  const [paletteOpen, setPaletteOpen] = React.useState(false)
  const runCommand = (label: string) => {
    setPaletteOpen(false)
    toast.add({
      title: label,
      description: "Command palette demo",
      type: "info",
    })
  }
  const [groupBy, setGroupBy] = React.useState("status")
  const [compact, setCompact] = React.useState(false)
  const [hideEmpty, setHideEmpty] = React.useState(true)
  const [status, setStatus] = React.useState("all")
  return (
    <SectionPage
      icon="layers"
      eyebrow="Primitives"
      title="Menus & overlays"
      description="Light 180px menus with a 9px radius and 32px, 12px items; dark tooltips; a 560px sheet for create/inspect flows and a compact dialog for focused tasks."
    >
      <Specimen
        title="Dropdown menus"
        description="Toolbar menus mix radio groups, checkboxes and small captions."
        code={`<DropdownMenu>
  <DropdownMenuTrigger render={<ToolbarButton icon="dashboard" value="Status" />}>
    Group by
  </DropdownMenuTrigger>
  <DropdownMenuContent>
    <DropdownMenuRadioGroup value={groupBy} onValueChange={setGroupBy}>
      <DropdownMenuRadioItem closeOnClick value="status">Status</DropdownMenuRadioItem>
      <DropdownMenuRadioItem closeOnClick value="repository">Repository</DropdownMenuRadioItem>
    </DropdownMenuRadioGroup>
  </DropdownMenuContent>
</DropdownMenu>`}
      >
        <Row className="gap-[7px]">
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
                <DropdownMenuRadioItem closeOnClick value="repository">
                  Repository
                </DropdownMenuRadioItem>
                <DropdownMenuRadioItem closeOnClick value="none">
                  No grouping
                </DropdownMenuRadioItem>
              </DropdownMenuRadioGroup>
            </DropdownMenuContent>
          </DropdownMenu>
          <DropdownMenu>
            <DropdownMenuTrigger render={<ToolbarButton icon="settings" />}>
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
              <DropdownMenuSeparator />
              <DropdownMenuGroup>
                <DropdownMenuItem>Collapse all groups</DropdownMenuItem>
                <DropdownMenuItem>Expand all groups</DropdownMenuItem>
              </DropdownMenuGroup>
            </DropdownMenuContent>
          </DropdownMenu>
          <DropdownMenu>
            <DropdownMenuTrigger
              render={<ToolbarButton icon="filter" active={status !== "all"} />}
            >
              Filter
            </DropdownMenuTrigger>
            <DropdownMenuContent className="min-w-52">
              <DropdownMenuGroup>
                <DropdownMenuLabel>Status</DropdownMenuLabel>
                <DropdownMenuRadioGroup
                  value={status}
                  onValueChange={(value) => setStatus(String(value))}
                >
                  <DropdownMenuRadioItem closeOnClick value="all">
                    All statuses
                  </DropdownMenuRadioItem>
                  <DropdownMenuRadioItem closeOnClick value="running">
                    Running
                  </DropdownMenuRadioItem>
                  <DropdownMenuRadioItem closeOnClick value="completed">
                    Completed
                  </DropdownMenuRadioItem>
                  <DropdownMenuRadioItem closeOnClick value="failed">
                    Failed
                  </DropdownMenuRadioItem>
                </DropdownMenuRadioGroup>
              </DropdownMenuGroup>
              <DropdownMenuSeparator />
              <DropdownMenuGroup>
                <DropdownMenuItem onClick={() => setStatus("all")}>
                  Clear filters
                </DropdownMenuItem>
              </DropdownMenuGroup>
            </DropdownMenuContent>
          </DropdownMenu>
          <DropdownMenu>
            <DropdownMenuTrigger
              render={
                <Button
                  variant="ghost"
                  size="icon-xs"
                  aria-label="Workspace options"
                />
              }
            >
              <Icon icon="more" />
            </DropdownMenuTrigger>
            <DropdownMenuContent className="min-w-52">
              <DropdownMenuGroup>
                <DropdownMenuItem>
                  <Icon icon="layers" />
                  Preview example tasks
                </DropdownMenuItem>
                <DropdownMenuItem>
                  <Icon icon="robot" />
                  View agent plans
                </DropdownMenuItem>
                <DropdownMenuItem>
                  <Icon icon="link" />
                  Copy dashboard link
                  <DropdownMenuShortcut>⌘L</DropdownMenuShortcut>
                </DropdownMenuItem>
              </DropdownMenuGroup>
              <DropdownMenuSeparator />
              <DropdownMenuGroup>
                <DropdownMenuItem variant="destructive">
                  <Icon icon="stop" />
                  Cancel run
                </DropdownMenuItem>
              </DropdownMenuGroup>
            </DropdownMenuContent>
          </DropdownMenu>
        </Row>
      </Specimen>

      <Specimen
        title="Command palette"
        description="cmdk on the same light menu surface: a search field, quiet group headings and 32px items with shortcuts. Inline in a panel or popover, or as a dialog behind ⌘K."
        code={`<CommandDialog open={open} onOpenChange={setOpen}>
  <Command>
    <CommandInput placeholder="Search commands…" />
    <CommandList>
      <CommandEmpty>No commands found.</CommandEmpty>
      <CommandGroup heading="Tasks">
        <CommandItem onSelect={createTask}>
          <Icon icon="plus" />
          New task
          <CommandShortcut>N</CommandShortcut>
        </CommandItem>
      </CommandGroup>
    </CommandList>
  </Command>
</CommandDialog>`}
      >
        <Row className="items-start gap-6">
          <Command className="w-[320px] border border-border shadow-lg">
            <DemoCommands onRun={runCommand} />
          </Command>
          <Button variant="outline" onClick={() => setPaletteOpen(true)}>
            <Icon icon="command" data-icon="inline-start" />
            Open palette
          </Button>
          <CommandDialog open={paletteOpen} onOpenChange={setPaletteOpen}>
            <Command>
              <DemoCommands onRun={runCommand} />
            </Command>
          </CommandDialog>
        </Row>
      </Specimen>

      <Specimen
        title="Tooltips"
        description="Dark, compact labels. Icon-only controls always carry one."
      >
        <Row className="gap-4">
          {(["top", "right", "bottom", "left"] as const).map((side) => (
            <Tooltip key={side}>
              <TooltipTrigger
                render={
                  <Button variant="outline" size="sm" className="capitalize" />
                }
              >
                {side}
              </TooltipTrigger>
              <TooltipContent side={side}>
                Refresh tasks <Kbd>R</Kbd>
              </TooltipContent>
            </Tooltip>
          ))}
        </Row>
      </Specimen>

      <Specimen
        title="Sheet"
        description="Creation and inspection happen in an accessible 560px side sheet with a spaced eyebrow and a large title."
        code={`<Sheet>
  <SheetTrigger render={<Button />}>Agent plans</SheetTrigger>
  <TaskSheetContent>
    <TaskSheetHeader eyebrow="Coding workspace" eyebrowIcon="robot"
      title="Agent plans" description="Reusable agent teams…" />
    …
  </TaskSheetContent>
</Sheet>`}
      >
        <Sheet>
          <SheetTrigger render={<Button variant="outline" />}>
            <Icon icon="robot" data-icon="inline-start" />
            Open task details
          </SheetTrigger>
          <TaskSheetContent>
            <TaskSheetHeader
              eyebrow="FG-214 · Coding workspace"
              eyebrowIcon="task"
              title="Test worker reconnection"
              description="Implement the requested change, cover edge cases with tests, and have the configured reviewer check the changes before committing."
            />
            <DetailList>
              <DetailRow label="Status">
                <StatusBadge status="review" />
              </DetailRow>
              <DetailRow label="Repository">workspace/coding-agent</DetailRow>
              <DetailRow label="Base branch">main</DetailRow>
              <DetailRow label="Agents">
                <AgentAvatars agents={agents} />
              </DetailRow>
              <DetailRow label="Updated">Sep 8, 2026</DetailRow>
            </DetailList>
          </TaskSheetContent>
        </Sheet>
      </Specimen>

      <Specimen
        title="Dialog"
        description="Focused tasks (Open PR, Request changes) use a 560px dialog at the 14px UI size with the run identified up front."
      >
        <Dialog>
          <DialogTrigger render={<Button variant="outline" size="sm" />}>
            <Icon icon="review" data-icon="inline-start" />
            Open PR
          </DialogTrigger>
          <DialogContent className="max-h-[calc(100dvh-32px)] w-[min(560px,calc(100%-32px))] gap-6 overflow-y-auto text-sm/[1.5] sm:max-w-none">
            <DialogHeader className="gap-2.5 pr-5">
              <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                <Icon icon="branch" size={14} />
                workspace/coding-agent · Run #3
              </div>
              <DialogTitle className="text-xl/[1.3]">
                Open a pull request
              </DialogTitle>
              <DialogDescription className="text-sm">
                Publishes the run's saved snapshot to a new branch and opens a
                pull request.
              </DialogDescription>
            </DialogHeader>
            <FieldGroup className="gap-5">
              <Field>
                <FieldLabel htmlFor="pr-target" className="text-sm">
                  Target branch
                </FieldLabel>
                <Input
                  id="pr-target"
                  defaultValue="main"
                  className="h-[38px] text-sm md:text-sm"
                />
              </Field>
              <Field>
                <FieldLabel htmlFor="pr-branch" className="text-sm">
                  New branch name
                </FieldLabel>
                <Input
                  id="pr-branch"
                  defaultValue="forge/test-worker-reconnection"
                  className="h-[38px] text-sm md:text-sm"
                />
              </Field>
              <Field>
                <FieldLabel htmlFor="pr-message" className="text-sm">
                  Commit message
                </FieldLabel>
                <Textarea
                  id="pr-message"
                  defaultValue="Test worker reconnection"
                  className="min-h-28 resize-y text-sm md:text-sm"
                />
              </Field>
            </FieldGroup>
            <DialogFooter>
              <DialogClose render={<Button variant="outline" />}>
                Cancel
              </DialogClose>
              <Button>
                <Icon icon="review" data-icon="inline-start" />
                Open pull request
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      </Specimen>
    </SectionPage>
  )
}

/* -------------------------------------------------------------------------- */
/* Tabs & toggles                                                             */
/* -------------------------------------------------------------------------- */

export function TabsSection() {
  const [layout, setLayout] = React.useState("list")
  return (
    <SectionPage
      icon="dashboard"
      eyebrow="Primitives"
      title="Tabs & toggles"
      description="Three tab treatments: borderless view tabs with a hairline ring for the active view, underlined line tabs for detail pages, and the stock segmented tabs. The layout switch is a compact toggle group."
    >
      <Specimen
        title="View tabs"
        code={`<Tabs value={view} onValueChange={setView}>
  <ViewTabsList aria-label="Task views">
    <ViewTabsTrigger value="list" icon="list">List</ViewTabsTrigger>
    <ViewTabsTrigger value="kanban" icon="kanban">Kanban</ViewTabsTrigger>
  </ViewTabsList>
</Tabs>`}
      >
        <Tabs defaultValue="list">
          <ViewTabsList aria-label="Task views">
            <ViewTabsTrigger value="list" icon="list">
              List
            </ViewTabsTrigger>
            <ViewTabsTrigger value="kanban" icon="kanban">
              Kanban
            </ViewTabsTrigger>
            <ViewTabsTrigger value="activity" icon="activity">
              Activity
            </ViewTabsTrigger>
            <ViewTabsTrigger value="dashboard" icon="dashboard">
              Dashboard
            </ViewTabsTrigger>
          </ViewTabsList>
        </Tabs>
      </Specimen>
      <Specimen
        title="Line tabs"
        description="Detail-page tabs at the 14px UI size, with an optional live dot."
        variant="flush"
      >
        <Tabs defaultValue="logs" className="gap-0">
          <div className="border-b px-6">
            <TaskTabsList aria-label="Run pages">
              <TaskTabsTrigger value="submission" icon="task">
                Submission
              </TaskTabsTrigger>
              <TaskTabsTrigger value="logs" icon="activity" live>
                Run logs
              </TaskTabsTrigger>
              <TaskTabsTrigger value="changes" icon="review">
                Changes
              </TaskTabsTrigger>
              <TaskTabsTrigger value="handoff" icon="layers">
                Handoff
              </TaskTabsTrigger>
              <TaskTabsTrigger value="usage" icon="dashboard">
                Token Usage / Cost
              </TaskTabsTrigger>
            </TaskTabsList>
          </div>
        </Tabs>
      </Specimen>
      <Specimen title="Segmented tabs (stock)">
        <Tabs defaultValue="account" className="max-w-md">
          <TabsList>
            <TabsTrigger value="account">Account</TabsTrigger>
            <TabsTrigger value="workspace">Workspace</TabsTrigger>
            <TabsTrigger value="billing">Billing</TabsTrigger>
          </TabsList>
          <TabsContent
            value="account"
            className="pt-2 text-xs text-muted-foreground"
          >
            Manage your account preferences.
          </TabsContent>
          <TabsContent
            value="workspace"
            className="pt-2 text-xs text-muted-foreground"
          >
            Workspace members and defaults.
          </TabsContent>
          <TabsContent
            value="billing"
            className="pt-2 text-xs text-muted-foreground"
          >
            Plans and invoices.
          </TabsContent>
        </Tabs>
      </Specimen>
      <Specimen
        title="Layout switch & toggle group"
        code={`<LayoutSwitch value={layout} onValueChange={setLayout} options={[
  { value: "kanban", label: "Kanban layout", icon: "kanban" },
  { value: "list", label: "List layout", icon: "list" },
]} />`}
      >
        <Row className="gap-6">
          <LayoutSwitch
            value={layout}
            onValueChange={setLayout}
            options={[
              { value: "kanban", label: "Kanban layout", icon: "kanban" },
              { value: "list", label: "List layout", icon: "list" },
            ]}
          />
          <ToggleGroup
            defaultValue={["split"]}
            variant="outline"
            aria-label="Diff layout"
          >
            <ToggleGroupItem value="unified" size="sm">
              Unified
            </ToggleGroupItem>
            <ToggleGroupItem value="split" size="sm">
              Split
            </ToggleGroupItem>
          </ToggleGroup>
        </Row>
      </Specimen>
    </SectionPage>
  )
}

/* -------------------------------------------------------------------------- */
/* Badges & status                                                            */
/* -------------------------------------------------------------------------- */

export function BadgesSection() {
  return (
    <SectionPage
      icon="completed"
      eyebrow="Primitives"
      title="Badges & status"
      description="State is the only place the palette gets loud: 19px pastel status badges, band symbols, tinted chips for results and verdicts, presence dots and coloured run icons."
    >
      <Specimen
        title="Status badges"
        code={`<StatusBadge status="running" />
<StatusBadge status="review" />
<StatusBadge status="completed">Completed · Approved</StatusBadge>
<StatusBadge status="review" size="lg" />   // detail pages`}
      >
        <div className="flex flex-col gap-4">
          <Row>
            <StatusBadge status="pending" />
            <StatusBadge status="enqueued" />
            <StatusBadge status="running" />
            <StatusBadge status="review" />
            <StatusBadge status="completed" />
            <StatusBadge status="failed" />
            <StatusBadge status="cancelled" />
          </Row>
          <Row>
            <StatusBadge status="running">Running · Implementing</StatusBadge>
            <StatusBadge status="completed">Completed · Approved</StatusBadge>
            <StatusBadge status="completed">
              Completed · Unresolved findings
            </StatusBadge>
            <StatusBadge status="review" size="lg" />
          </Row>
        </div>
      </Specimen>
      <Specimen
        title="Band symbols & counts"
        description="The glyph at the start of a status band; tone comes from the band or an explicit prop."
      >
        <Row className="gap-5">
          <StatusSymbol kind="backlog" tone="neutral" />
          <StatusSymbol kind="progress" tone="amber" />
          <StatusSymbol kind="review" tone="pink" />
          <StatusSymbol kind="completed" tone="green" />
          <StatusSymbol kind="failed" tone="red" />
          <CountBadge>5</CountBadge>
          <CountBadge>12</CountBadge>
        </Row>
      </Specimen>
      <Specimen
        title="Chips"
        description="Verification results, review verdicts, flags and roles."
        code={`<Chip tone="success" icon="completed">Approved</Chip>
<VerificationStatus result="unable_to_verify" />`}
      >
        <div className="flex flex-col gap-4">
          <Row>
            <VerificationStatus result="passed" />
            <VerificationStatus result="failed" />
            <VerificationStatus result="skipped" />
            <VerificationStatus result="not_run" />
            <VerificationStatus result="unable_to_verify" />
          </Row>
          <Row>
            <Chip tone="success" icon="completed">
              Approved
            </Chip>
            <Chip tone="warning" icon="warning">
              Needs changes
            </Chip>
            <Chip tone="danger" icon="failed">
              Failed
            </Chip>
            <Chip tone="notice">Waiting for a worker</Chip>
            <Chip tone="outline">reviewer</Chip>
            <Chip tone="neutral">Read-only example</Chip>
          </Row>
        </div>
      </Specimen>
      <Specimen title="Signals & presence">
        <div className="flex flex-col gap-4">
          <Row className="gap-6 text-xs">
            <span className="flex items-center gap-2">
              <ConnectionDot /> Live updates on
            </span>
            <span className="flex items-center gap-2">
              <ConnectionDot online={false} /> Reconnecting
            </span>
          </Row>
          <Row className="gap-6 text-xs">
            <span className="flex items-center gap-2">
              <RunStateIcon state="running" /> Running
            </span>
            <span className="flex items-center gap-2">
              <RunStateIcon state="completed" /> Completed
            </span>
            <span className="flex items-center gap-2">
              <RunStateIcon state="failed" /> Failed
            </span>
            <span className="flex items-center gap-2 text-muted-foreground">
              <RunStateIcon state="planned" /> Planned
            </span>
          </Row>
        </div>
      </Specimen>
      <Specimen
        title="Avatars & keys"
        description="Gradient orbs identify agents (with accessible names in tooltips); the square orb is the workspace mark."
        code={`<AgentAvatars agents={agents} />
<AgentAvatars agents={agents} size="lg" />
<AgentAvatars agents={[]} />`}
      >
        <Row className="gap-6">
          <AgentAvatars agents={agents} />
          <AgentAvatars agents={agents} size="lg" />
          <AgentAvatars
            agents={[
              ...agents,
              { id: "a4", name: "Deduplicator" },
              { id: "a5", name: "Explainer" },
              { id: "a6", name: "Security reviewer" },
              { id: "a7", name: "Docs writer" },
            ]}
          />
          <AgentAvatars agents={[]} />
          <span className="flex items-center gap-1">
            {[0, 1, 2, 3, 4].map((variant) => (
              <AgentOrb key={variant} variant={variant} size="lg" />
            ))}
          </span>
          <WorkspaceOrb />
          <WorkspaceOrb size="lg" />
          <Kbd>⌘K</Kbd>
          <Kbd variant="ghost">⌘ K</Kbd>
          <Kbd variant="outline">N</Kbd>
        </Row>
      </Specimen>
    </SectionPage>
  )
}

/* -------------------------------------------------------------------------- */
/* Feedback                                                                   */
/* -------------------------------------------------------------------------- */

export function FeedbackSection() {
  return (
    <SectionPage
      icon="info"
      eyebrow="Primitives"
      title="Feedback"
      description="Loading, empty, failure and success are always explicit. Errors are recoverable and say what happened; notices appear bottom-centre and dismiss themselves."
    >
      <Specimen
        title="Error callouts"
        code={`<ErrorCallout
  title="Couldn't load tasks"
  action={<Button variant="outline">Retry</Button>}
>
  The admin API did not respond. Check that it is running.
</ErrorCallout>`}
      >
        <div className="flex max-w-3xl flex-col gap-4">
          <ErrorCallout
            title="Couldn’t load tasks"
            action={<Button variant="outline">Retry</Button>}
          >
            The admin API did not respond at http://localhost:3001. Check that
            it is running, then retry.
          </ErrorCallout>
          <ErrorCallout>
            Describe the task in at least 8 characters.
          </ErrorCallout>
          <Alert>
            <Icon icon="info" />
            <AlertTitle>Queue dispatch is pending</AlertTitle>
            <AlertDescription>
              The task was saved and will be queued when a worker connects.
            </AlertDescription>
          </Alert>
        </div>
      </Specimen>
      <Specimen
        title="Notices"
        description="Toast notifications from the shadcn Base UI toast manager, restyled to the workspace's bottom-centre notice."
        code={`import { toast } from "@/components/ui/toast"

toast.add({ title: "Task link copied.", type: "info" })`}
      >
        <Row>
          <Button
            variant="outline"
            onClick={() =>
              toast.add({ title: "Task link copied.", type: "info" })
            }
          >
            Show notice
          </Button>
          <Button
            variant="outline"
            onClick={() =>
              toast.add({
                title: "Task created",
                description: "It was added to the queue.",
                type: "success",
              })
            }
          >
            Success
          </Button>
          <Button
            variant="outline"
            onClick={() =>
              toast.add({
                title: "Could not copy the link",
                description: "Copy it from the address bar.",
                type: "error",
              })
            }
          >
            Error
          </Button>
        </Row>
      </Specimen>
      <Specimen title="Loading">
        <div className="flex flex-col gap-6">
          <Row className="gap-6 text-xs text-muted-foreground">
            <span className="flex items-center gap-2">
              <Spinner /> Loading saved activity…
            </span>
            <Progress value={62} className="w-64" />
          </Row>
          <div className="max-w-3xl">
            <TaskListSkeleton groups={1} rows={3} />
          </div>
        </div>
      </Specimen>
      <Specimen
        title="Banners & list helpers"
        code={`<PreviewBanner label="Sample workspace" explanation="Explore the layout with example tasks"
  action="Back to live tasks" onAction={…} />
<ActiveFilters onClear={…}>3 matching tasks · workspace/web</ActiveFilters>
<LoadMore label="Load more tasks" hint="Search covers all tasks." />`}
      >
        <div className="max-w-3xl">
          <PreviewBanner
            label="Sample workspace"
            explanation="Explore the layout with example tasks"
            action="Back to live tasks"
          />
          <ActiveFilters>
            3 matching tasks · workspace/web · Running
          </ActiveFilters>
          <LoadMore
            label="Load more tasks"
            hint="Search covers all tasks. Agent, repository, and sort controls apply to loaded tasks."
          />
        </div>
      </Specimen>
    </SectionPage>
  )
}
