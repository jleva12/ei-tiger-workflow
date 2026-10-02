import * as React from "react"
import { cn } from "cn"

import { Button } from "@/components/ui/button"
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible"
import { Skeleton } from "@/components/ui/skeleton"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import {
  AgentAvatars,
  EmptyPersonAvatar,
  PersonAvatar,
  type Agent,
} from "./avatars"
import { Icon } from "./icon"
import type { IconProp } from "./icons"
import { CountBadge, StatusBadge, StatusSymbol } from "./status"
import {
  symbolTones,
  toneVars,
  type SymbolKind,
  type TaskStatus,
  type Tone,
} from "./variants"

/** The person a task is assigned to. */
type TaskAssignee = {
  id: string
  name: string
}

type TaskItem = {
  id: string
  title: string
  /** Secondary text under the title on cards, e.g. the task's description. */
  summary?: string
  status: TaskStatus
  /** Overrides the default status label (e.g. "Running · Implementing"). */
  statusLabel?: string
  repository?: string
  /** Display-ready date, e.g. "Sep 8, 2026". */
  updated?: string
  agents?: Agent[]
  runCount?: number
  /**
   * Who it's assigned to; `null` when nobody is. Leave it out where tasks
   * aren't assigned, and cards show no assignee.
   */
  assignee?: TaskAssignee | null
}

/** Wraps task groups; `density="compact"` tightens rows and group spacing. */
function TaskList({
  density = "default",
  className,
  ...props
}: React.ComponentProps<"div"> & { density?: "default" | "compact" }) {
  return (
    <div
      data-slot="task-list"
      data-density={density}
      className={cn("group/tasklist", className)}
      {...props}
    />
  )
}

/**
 * A collapsible status group: a full-width pastel band (symbol, monospace
 * title, count, actions) followed by its rows or cards.
 */
function TaskGroup({
  title,
  count,
  symbol = "backlog",
  tone,
  actions,
  layout = "list",
  open,
  defaultOpen = true,
  onOpenChange,
  className,
  children,
}: {
  title: string
  count?: number
  symbol?: SymbolKind
  /** Defaults to the symbol's tone (progress → amber, review → pink…). */
  tone?: Tone
  actions?: React.ReactNode
  layout?: "list" | "board"
  open?: boolean
  defaultOpen?: boolean
  onOpenChange?: (open: boolean) => void
  className?: string
  children?: React.ReactNode
}) {
  return (
    <Collapsible
      open={open}
      defaultOpen={defaultOpen}
      onOpenChange={onOpenChange}
      render={<section aria-label={title} />}
      data-slot="task-group"
      className={cn(
        toneVars[tone ?? symbolTones[symbol]],
        "mb-[21px] group-data-[density=compact]/tasklist:mb-3.5 @min-[1700px]/shell:mb-6",
        layout === "board" && "mb-0 w-[295px] shrink-0",
        className
      )}
    >
      <div
        data-slot="task-group-band"
        className={cn(
          "group/band flex h-[39px] items-center gap-[3px] rounded-(--radius-band) bg-(--tone) pr-3 pl-[17px] text-muted-foreground",
          layout === "board" && "pl-3"
        )}
      >
        <h2 className="flex h-full min-w-0 flex-1">
          <CollapsibleTrigger className="group/toggle flex h-full w-full min-w-0 items-center gap-[9px] text-left">
            <StatusSymbol kind={symbol} />
            <span className="truncate font-mono text-xs font-[450]">
              {title}
            </span>
            {count !== undefined && <CountBadge>{count}</CountBadge>}
            <Icon
              icon="right"
              size={13}
              className="shrink-0 text-subtle opacity-0 transition-[opacity,transform] duration-150 group-hover/band:opacity-100 group-focus-visible/toggle:opacity-100 group-data-panel-open/toggle:rotate-90"
            />
          </CollapsibleTrigger>
        </h2>
        {actions}
      </div>
      <CollapsibleContent>{children}</CollapsibleContent>
    </Collapsible>
  )
}

/** Ghost icon button for a group band (more, add…). */
function TaskGroupAction({
  icon,
  className,
  ...props
}: React.ComponentProps<typeof Button> & { icon: IconProp }) {
  return (
    <Button variant="ghost" size="icon-xs" className={className} {...props}>
      <Icon icon={icon} size={15} />
    </Button>
  )
}

const cellClass =
  "h-(--forge-row-height,45px) border-y border-transparent px-[17px] py-0 text-xs group-hover/row:border-border group-hover/row:bg-[color-mix(in_oklch,var(--muted)_60%,var(--background))] first:rounded-l-(--radius-card) first:border-l last:rounded-r-(--radius-card) last:border-r last:pl-0 group-data-[density=compact]/tasklist:h-[34px] @min-[1700px]/shell:h-[calc(var(--forge-row-height,45px)+3px)]"

function TaskTableHead({
  icon = "sort",
  className,
  children,
  ...props
}: React.ComponentProps<typeof TableHead> & { icon?: IconProp | null }) {
  return (
    <TableHead
      className={cn(
        "h-(--forge-row-height,45px) px-[17px] text-2xs font-normal text-muted-foreground",
        className
      )}
      {...props}
    >
      {children}
      {icon && (
        <Icon
          icon={icon}
          size={13}
          className="ml-1 inline-block align-[-2px] text-subtle"
        />
      )}
    </TableHead>
  )
}

function TaskTableRow({
  className,
  ...props
}: React.ComponentProps<typeof TableRow>) {
  return (
    <TableRow
      className={cn("group/row border-0 hover:bg-transparent", className)}
      {...props}
    />
  )
}

function TaskTableCell({
  className,
  ...props
}: React.ComponentProps<typeof TableCell>) {
  return <TableCell className={cn(cellClass, className)} {...props} />
}

/**
 * ID, title and run count; the whole cell is the row's primary action. A
 * `summary` joins the title in its tooltip.
 */
function TaskTitle({
  id,
  title,
  summary,
  runCount,
  className,
  ...props
}: React.ComponentProps<"button"> & {
  id: string
  title: string
  summary?: string
  runCount?: number
}) {
  return (
    <button
      type="button"
      title={summary ? `${title}\n${summary}` : title}
      className={cn(
        "flex h-[calc(var(--forge-row-height,45px)-3px)] w-full min-w-0 items-center gap-[15px] text-left group-data-[density=compact]/tasklist:h-8",
        className
      )}
      {...props}
    >
      <span className="w-[63px] shrink-0 truncate font-mono text-xs text-subtle">
        {id}
      </span>
      <span className="truncate text-[0.8125rem] font-[430] @min-[1700px]/shell:text-sm">
        {title}
      </span>
      {(runCount ?? 0) > 1 && (
        <span
          className="flex shrink-0 items-center gap-1 text-2xs text-subtle"
          aria-label={`${runCount} runs`}
        >
          <Icon icon="layers" size={14} />
          {runCount}
        </span>
      )}
    </button>
  )
}

function RepoLabel({
  className,
  children,
  ...props
}: React.ComponentProps<"span">) {
  return (
    <span
      className={cn("flex min-w-0 items-center gap-1.5 text-2xs", className)}
      {...props}
    >
      <Icon icon="branch" size={14} className="shrink-0" />
      <span className="truncate">{children}</span>
    </span>
  )
}

function DateLabel({
  className,
  children,
  ...props
}: React.ComponentProps<"span">) {
  return (
    <span
      className={cn(
        "flex items-center gap-[7px] text-2xs whitespace-nowrap text-muted-foreground",
        className
      )}
      {...props}
    >
      <Icon icon="calendar" size={14} className="text-subtle" />
      {children}
    </span>
  )
}

/** A task's assignee: their avatar and name, or a quiet "Unassigned". */
function AssigneeLabel({
  assignee,
  className,
  ...props
}: React.ComponentProps<"span"> & {
  assignee: TaskAssignee | null | undefined
}) {
  return (
    <span
      data-slot="assignee-label"
      title={assignee?.name}
      className={cn(
        "flex min-w-0 items-center gap-[7px] text-2xs",
        assignee ? "text-muted-foreground" : "text-subtle",
        className
      )}
      {...props}
    >
      {assignee ? <PersonAvatar name={assignee.name} /> : <EmptyPersonAvatar />}
      <span className="truncate">{assignee?.name ?? "Unassigned"}</span>
    </span>
  )
}

/**
 * The flat task table used inside a TaskGroup: name, status, repository,
 * updated and agents, and with `showAssignee` each task's assignee after
 * the repository. Scrolls horizontally below 760px (860px with assignees).
 */
function TaskTable({
  tasks,
  onSelect,
  showAssignee = false,
  emptyLabel = "No tasks here yet",
  className,
}: {
  tasks: TaskItem[]
  onSelect?: (task: TaskItem) => void
  /** Add an Assignee column (from each task's `assignee`). */
  showAssignee?: boolean
  emptyLabel?: string
  className?: string
}) {
  return (
    <Table
      data-slot="task-table"
      className={cn(
        "table-fixed border-separate border-spacing-0",
        showAssignee
          ? "min-w-[860px]"
          : "min-w-[760px] @max-[800px]/shell:min-w-[800px]",
        className
      )}
    >
      {showAssignee ? (
        <colgroup>
          <col className="w-[38%] @max-[1270px]/shell:w-[34%]" />
          <col className="w-[12%] @max-[1270px]/shell:w-[13%]" />
          <col className="w-[16%] @max-[1270px]/shell:w-[17%]" />
          <col className="w-[14%] @max-[1270px]/shell:w-[15%]" />
          <col className="w-[13%]" />
          <col className="w-[7%] @max-[1270px]/shell:w-[8%]" />
        </colgroup>
      ) : (
        <colgroup>
          <col className="w-[46%] @max-[1270px]/shell:w-[43%]" />
          <col className="w-[12%]" />
          <col className="w-[19%] @max-[1270px]/shell:w-[21%]" />
          <col className="w-[16%]" />
          <col className="w-[7%] @max-[1270px]/shell:w-[8%]" />
        </colgroup>
      )}
      <TableHeader className="[&_tr]:border-0">
        <TableRow className="border-0 hover:bg-transparent">
          <TaskTableHead>Name</TaskTableHead>
          <TaskTableHead>Status</TaskTableHead>
          <TaskTableHead>Repository</TaskTableHead>
          {showAssignee && <TaskTableHead>Assignee</TaskTableHead>}
          <TaskTableHead icon="clock">Updated</TaskTableHead>
          <TaskTableHead className="pr-[17px] pl-0 text-right">
            Agents
          </TaskTableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {tasks.map((task) => (
          <TaskTableRow key={task.id}>
            <TaskTableCell>
              <TaskTitle
                id={task.id}
                title={task.title}
                summary={task.summary}
                runCount={task.runCount}
                onClick={() => onSelect?.(task)}
              />
            </TaskTableCell>
            <TaskTableCell>
              <StatusBadge status={task.status}>{task.statusLabel}</StatusBadge>
            </TaskTableCell>
            <TaskTableCell>
              <RepoLabel title={task.repository}>
                {task.repository ?? "No repository"}
              </RepoLabel>
            </TaskTableCell>
            {showAssignee && (
              <TaskTableCell>
                <AssigneeLabel assignee={task.assignee} />
              </TaskTableCell>
            )}
            <TaskTableCell>
              <DateLabel>{task.updated ?? "Not started"}</DateLabel>
            </TaskTableCell>
            <TaskTableCell>
              <AgentAvatars agents={task.agents ?? []} hideEmptyLabel />
            </TaskTableCell>
          </TaskTableRow>
        ))}
        {!tasks.length && (
          <TableRow className="border-0 hover:bg-transparent">
            <TableCell
              colSpan={showAssignee ? 6 : 5}
              className="h-[34px] px-[18px] pt-[5px] pb-[13px] text-2xs text-subtle"
            >
              {emptyLabel}
            </TableCell>
          </TableRow>
        )}
      </TableBody>
    </Table>
  )
}

/** Loading placeholder shaped like three task groups. */
function TaskListSkeleton({
  groups = 3,
  rows = 3,
}: {
  groups?: number
  rows?: number
}) {
  return (
    <div
      className="flex flex-col gap-[30px]"
      role="status"
      aria-label="Loading tasks"
    >
      {Array.from({ length: groups }, (_, group) => (
        <div key={group}>
          <Skeleton className="mb-5 h-10 w-full" />
          {Array.from({ length: rows }, (_, row) => (
            <div key={row} className="flex items-center justify-between p-4">
              <Skeleton className="h-4 w-1/2" />
              <Skeleton className="h-4 w-16" />
              <Skeleton className="h-4 w-24" />
            </div>
          ))}
        </div>
      ))}
    </div>
  )
}

export {
  TaskList,
  TaskGroup,
  TaskGroupAction,
  TaskTable,
  TaskTableHead,
  TaskTableRow,
  TaskTableCell,
  TaskTitle,
  RepoLabel,
  DateLabel,
  AssigneeLabel,
  TaskListSkeleton,
  type TaskItem,
  type TaskAssignee,
}
