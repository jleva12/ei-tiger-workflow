import * as React from "react"
import { cn } from "cn"

import { AgentAvatars } from "./avatars"
import { StatusBadge } from "./status"
import { AssigneeLabel, RepoLabel, TaskGroup, type TaskItem } from "./task-list"
import type { SymbolKind, Tone } from "./variants"

type KanbanColumnData = {
  id: string
  title: string
  symbol?: SymbolKind
  tone?: Tone
  tasks: TaskItem[]
}

/** Horizontal row of 295px status columns. */
function KanbanBoard({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="kanban-board"
      className={cn("flex min-h-full items-start gap-4", className)}
      {...props}
    />
  )
}

/** A status column: the same pastel band as the list, then stacked cards. */
function KanbanColumn({
  emptyLabel = "No tasks here yet",
  children,
  ...props
}: Omit<React.ComponentProps<typeof TaskGroup>, "layout"> & {
  emptyLabel?: string
}) {
  const empty = React.Children.count(children) === 0
  return (
    <TaskGroup layout="board" {...props}>
      <div className="flex flex-col gap-2.5 py-3">
        {children}
        {empty && (
          <p className="h-[34px] px-[18px] pt-[5px] pb-[13px] text-2xs text-subtle">
            {emptyLabel}
          </p>
        )}
      </div>
    </TaskGroup>
  )
}

/**
 * A task card: ID and status, title (and its summary), repository, the
 * assignee where tasks have one, date and agents.
 */
function BoardCard({
  task,
  className,
  ...props
}: React.ComponentProps<"button"> & { task: TaskItem }) {
  return (
    <button
      type="button"
      data-slot="board-card"
      className={cn(
        "rounded-(--radius-card) border bg-background p-[15px] text-left transition-colors hover:bg-muted",
        className
      )}
      {...props}
    >
      <span className="flex items-center justify-between gap-2">
        <span className="truncate font-mono text-xs text-subtle">
          {task.id}
        </span>
        <StatusBadge status={task.status}>{task.statusLabel}</StatusBadge>
      </span>
      <span
        className={cn(
          "mt-3.5 block text-[0.8125rem] leading-[1.6] font-[450]",
          task.summary ? "mb-1" : "mb-3"
        )}
      >
        {task.title}
      </span>
      {task.summary && (
        <span className="mb-3 line-clamp-2 block text-2xs leading-normal text-muted-foreground">
          {task.summary}
        </span>
      )}
      <RepoLabel className="text-muted-foreground">
        {task.repository ?? "No repository"}
      </RepoLabel>
      {task.assignee !== undefined && (
        <AssigneeLabel assignee={task.assignee} className="mt-2" />
      )}
      <span className="mt-[22px] flex items-center justify-between text-3xs text-subtle">
        <span>{task.updated ?? "Not started"}</span>
        <AgentAvatars agents={task.agents ?? []} />
      </span>
    </button>
  )
}

/** Data-driven board: one column per status with its task cards. */
function TaskBoard({
  columns,
  onSelect,
  actions,
  className,
}: {
  columns: KanbanColumnData[]
  onSelect?: (task: TaskItem) => void
  /** Band actions for a column (e.g. more / add buttons). */
  actions?: (column: KanbanColumnData) => React.ReactNode
  className?: string
}) {
  return (
    <KanbanBoard className={className}>
      {columns.map((column) => (
        <KanbanColumn
          key={column.id}
          title={column.title}
          count={column.tasks.length}
          symbol={column.symbol}
          tone={column.tone}
          actions={actions?.(column)}
        >
          {column.tasks.map((task) => (
            <BoardCard
              key={task.id}
              task={task}
              onClick={() => onSelect?.(task)}
            />
          ))}
        </KanbanColumn>
      ))}
    </KanbanBoard>
  )
}

export {
  KanbanBoard,
  KanbanColumn,
  BoardCard,
  TaskBoard,
  type KanbanColumnData,
}
