import * as React from "react"
import { Queue02Icon } from "@hugeicons/core-free-icons"

import { PageEmpty } from "@/components/forge/empty-state"
import { ErrorCallout } from "@/components/forge/feedback"
import { Icon } from "@/components/forge/icon"
import type { IconProp } from "@/components/forge/icons"
import { useShellPage } from "@/components/forge/shell"
import { StatusBadge } from "@/components/forge/status"
import {
  LiveLabel,
  TaskMeta,
  TaskPage,
  TaskPageEyebrow,
  TaskPageHeader,
  TaskTabBar,
  TaskTabsList,
  TaskTabsTrigger,
  TaskTitleRow,
} from "@/components/forge/task-page"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Skeleton } from "@/components/ui/skeleton"
import { Spinner } from "@/components/ui/spinner"
import { Tabs, TabsContent } from "@/components/ui/tabs"
import { toast } from "@/components/ui/toast"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import type { MyOrganization } from "@/lib/access"
import {
  failureTitle,
  isBackgroundTaskTab,
  isLiveStatus,
  kindLabel,
  shortId,
  statusDisplay,
  taskTitle,
  taskTypeOf,
  useAbandonBackgroundTask,
  useBackgroundTask,
  useRestartBackgroundTask,
  useResubmitBackgroundTask,
  type BackgroundTaskDetail as Detail,
  type BackgroundTaskTab,
} from "@/lib/background-tasks"
import { formatRelative } from "@/lib/format"
import { usePageContext } from "@/lib/page-context"
import {
  BackgroundTaskActivity,
  BackgroundTaskAttempts,
  BackgroundTaskOverview,
  type ApprovalPanelOf,
} from "./background-task-panes"

/** Where a task page is listed: its crumb in the top bar and its way back. */
export type TaskPageSection = {
  label: string
  icon: IconProp
  /** The back button's label, e.g. "All background tasks". */
  back: string
  /** The title when there's no such task. */
  missing: string
}

/**
 * A tab a kind of task adds to its page, after Overview: e.g. an ADK
 * workflow run's Steps.
 */
export type TaskPane<Tab extends string = string> = {
  value: Tab
  label: string
  icon: IconProp
  /** Marked live while the task is in progress, as Attempts is. */
  live?: boolean
  content: (task: Detail) => React.ReactNode
}

const BACKGROUND_TASKS: TaskPageSection = {
  label: "Background tasks",
  icon: Queue02Icon,
  back: "All background tasks",
  missing: "Background task not found",
}

/**
 * The shell wraps pages in its padded, scrolling `PageContent`; a task page
 * is its own scroll container, edge to edge, so it fills that region.
 */
function FullBleed({ children }: { children: React.ReactNode }) {
  return <div className="absolute inset-0 flex flex-col">{children}</div>
}

function copyLink() {
  void navigator.clipboard.writeText(window.location.href).then(
    () => toast.add({ title: "Link copied.", type: "info" }),
    () => toast.add({ title: "Couldn't copy the link", type: "error" })
  )
}

/** Confirms giving up on the task; shows why if it can't. */
function AbandonDialog({
  open,
  onOpenChange,
  organizationId,
  taskId,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  organizationId: string
  taskId: string
}) {
  // The dialog shows the failure itself.
  const abandon = useAbandonBackgroundTask(organizationId, taskId, {
    meta: { silent: true },
  })
  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (abandon.isPending) return
        if (!next) abandon.reset()
        onOpenChange(next)
      }}
    >
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Abandon this task?</DialogTitle>
          <DialogDescription>
            It stops for good: the worker won't retry it, and nobody can retry
            it from here. You can still resubmit it, which runs it again as a
            new task.
          </DialogDescription>
        </DialogHeader>
        {abandon.error && (
          <ErrorCallout title="Couldn't abandon the task">
            {abandon.error.message}
          </ErrorCallout>
        )}
        <DialogFooter>
          <DialogClose
            render={<Button variant="outline" disabled={abandon.isPending} />}
          >
            Keep it
          </DialogClose>
          <Button
            variant="destructive"
            disabled={abandon.isPending}
            onClick={() =>
              abandon.mutate(undefined, {
                onSuccess: () => onOpenChange(false),
              })
            }
          >
            {abandon.isPending && <Spinner data-icon="inline-start" />}
            Abandon task
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

/**
 * Copy link, and for those who manage the organization's background tasks what the
 * task's state allows: Retry (its next attempt, the primary action),
 * Resubmit (a new task with the same input) and Abandon.
 */
function TaskActions({
  organizationId,
  task,
  canManage,
}: {
  organizationId: string
  task: Detail
  canManage: boolean
}) {
  const [abandoning, setAbandoning] = React.useState(false)
  const restart = useRestartBackgroundTask(organizationId, task.id)
  const resubmit = useResubmitBackgroundTask(organizationId, task.id)
  const busy = restart.isPending || resubmit.isPending
  const { actions } = task
  return (
    <>
      <Button variant="outline" size="sm" onClick={copyLink}>
        <Icon icon="link" data-icon="inline-start" />
        Copy link
      </Button>
      {canManage && actions.resubmit && (
        <Tooltip>
          <TooltipTrigger
            render={
              <Button
                variant="outline"
                size="sm"
                disabled={busy}
                onClick={() => resubmit.mutate()}
              />
            }
          >
            {resubmit.isPending ? (
              <Spinner data-icon="inline-start" />
            ) : (
              <Icon icon="plus" data-icon="inline-start" />
            )}
            Resubmit
          </TooltipTrigger>
          <TooltipContent className="max-w-72">
            Runs the same job with the same input again, as a new task.
          </TooltipContent>
        </Tooltip>
      )}
      {canManage && actions.restart && (
        <Tooltip>
          <TooltipTrigger
            render={
              <Button
                size="sm"
                disabled={busy}
                onClick={() => restart.mutate()}
              />
            }
          >
            {restart.isPending ? (
              <Spinner data-icon="inline-start" />
            ) : (
              <Icon icon="refresh" data-icon="inline-start" />
            )}
            Retry
          </TooltipTrigger>
          <TooltipContent className="max-w-72">
            Runs it again as attempt {task.attempts + 1}, skipping the steps it
            already finished.
          </TooltipContent>
        </Tooltip>
      )}
      {canManage && actions.abandon && (
        <Button
          variant="destructive"
          size="sm"
          disabled={busy}
          onClick={() => setAbandoning(true)}
        >
          <Icon icon="stop" data-icon="inline-start" />
          Abandon
        </Button>
      )}
      {/* Mounted while open: abandoning takes Abandon away, and the dialog
          still has to close itself. */}
      {canManage && (actions.abandon || abandoning) && (
        <AbandonDialog
          open={abandoning}
          onOpenChange={setAbandoning}
          organizationId={organizationId}
          taskId={task.id}
        />
      )}
    </>
  )
}

function HeaderSkeleton() {
  return (
    <TaskPageHeader>
      <Skeleton className="mb-[19px] h-7 w-48" />
      <Skeleton className="mb-3.5 h-8 w-[min(36rem,100%)]" />
      <div className="flex gap-3">
        <Skeleton className="h-6 w-24" />
        <Skeleton className="h-6 w-40" />
        <Skeleton className="h-6 w-20" />
      </div>
    </TaskPageHeader>
  )
}

/**
 * One of an organization's background tasks, under Background tasks in the top bar's
 * trail (or `section`'s: a workflow run's is under Workflows): its header
 * (status, type, attempts) with Copy link and, for those who manage
 * background tasks, Retry, Resubmit and Abandon as its state allows; then
 * three tabs — Overview (its input, result and record), Attempts (each run
 * with its failures and steps) and Activity (its audit trail) — and any
 * `panes` its kind adds after Overview. What it waits for is decided in
 * Overview, by `approvalPanel` (a workflow run's approval panel when
 * absent). It follows a task in progress every few seconds.
 */
export function BackgroundTaskDetail<Tab extends string = never>({
  organization,
  taskId,
  tab,
  canManage,
  section = BACKGROUND_TASKS,
  panes = [],
  approvalPanel,
  onTabChange,
  onBack,
}: {
  organization: MyOrganization
  taskId: string
  tab: BackgroundTaskTab | Tab
  /** `background_tasks:manage` in the organization: offer the actions. */
  canManage: boolean
  /** Where it's listed; Background tasks when absent. */
  section?: TaskPageSection
  /** Tabs its kind adds, after Overview. */
  panes?: TaskPane<Tab>[]
  /** What answers the approval it waits for; a workflow run's when absent. */
  approvalPanel?: ApprovalPanelOf
  onTabChange: (tab: BackgroundTaskTab | Tab) => void
  onBack: () => void
}) {
  const detail = useBackgroundTask(organization.id, taskId)
  const task = detail.data
  const reference = shortId(taskId)
  const shown =
    task && statusDisplay(task.status, task.outcome, task.waiting_until)

  useShellPage({
    header: {
      title: reference,
      icon: section.icon,
      breadcrumbs: [
        { label: organization.name, href: `/organizations/${organization.id}` },
        { label: section.label, icon: section.icon, onSelect: onBack },
      ],
    },
  })
  // For the assistant: the task and the tab.
  usePageContext({
    entities: [
      {
        kind: "background_task",
        id: taskId,
        label: task ? `${reference}: ${taskTitle(task)}` : reference,
        detail: shown?.label,
      },
    ],
    view: { tab },
  })

  if (!task || !shown) {
    if (detail.error?.status === 404) {
      return (
        <PageEmpty
          illustration="search"
          title={section.missing}
          description="It isn't one of this organization's tasks, or it no longer exists."
        >
          <Button variant="outline" size="sm" onClick={onBack}>
            <Icon icon="left" data-icon="inline-start" />
            {section.back}
          </Button>
        </PageEmpty>
      )
    }
    if (detail.error) {
      return (
        <ErrorCallout
          title={failureTitle(
            detail.error,
            "Couldn't load the background task"
          )}
          action={
            <Button
              variant="outline"
              size="sm"
              onClick={() => void detail.refetch()}
            >
              Retry
            </Button>
          }
        >
          {detail.error.message}
        </ErrorCallout>
      )
    }
    return (
      <FullBleed>
        <TaskPage aria-busy="true">
          <HeaderSkeleton />
        </TaskPage>
      </FullBleed>
    )
  }

  const live = isLiveStatus(task.status)
  const type = taskTypeOf(task.task_type)

  return (
    <FullBleed>
      <TaskPage>
        <TaskPageHeader>
          <TaskPageEyebrow
            onBack={onBack}
            backLabel={section.back}
            reference={reference}
          />
          <TaskTitleRow
            title={taskTitle(task)}
            meta={
              <>
                <StatusBadge status={shown.status} size="lg">
                  {shown.label}
                </StatusBadge>
                <TaskMeta icon={type.icon}>
                  {type.label} · {kindLabel(task.kind)}
                </TaskMeta>
                <TaskMeta icon="refresh">
                  {task.attempts === 1
                    ? "1 attempt"
                    : `${task.attempts} attempts`}
                </TaskMeta>
                <TaskMeta icon="clock">
                  Updated {formatRelative(task.updated_at)}
                </TaskMeta>
              </>
            }
            actions={
              <TaskActions
                organizationId={organization.id}
                task={task}
                canManage={canManage}
              />
            }
          />
        </TaskPageHeader>
        <Tabs
          value={tab}
          onValueChange={(value) => {
            if (isBackgroundTaskTab(value)) onTabChange(value)
            else {
              const pane = panes.find((p) => p.value === value)
              if (pane) onTabChange(pane.value)
            }
          }}
          className="gap-0"
        >
          <TaskTabBar>
            <TaskTabsList aria-label="Background task pages">
              <TaskTabsTrigger value="overview" icon="task">
                Overview
              </TaskTabsTrigger>
              {panes.map((pane) => (
                <TaskTabsTrigger
                  key={pane.value}
                  value={pane.value}
                  icon={pane.icon}
                  live={pane.live && live}
                >
                  {pane.label}
                </TaskTabsTrigger>
              ))}
              <TaskTabsTrigger value="attempts" icon="refresh" live={live}>
                Attempts
              </TaskTabsTrigger>
              <TaskTabsTrigger value="activity" icon="activity">
                Activity
              </TaskTabsTrigger>
            </TaskTabsList>
            {live && <LiveLabel>Live</LiveLabel>}
          </TaskTabBar>
          <TabsContent value="overview">
            <BackgroundTaskOverview
              organizationId={organization.id}
              task={task}
              approvalPanel={approvalPanel}
            />
          </TabsContent>
          {panes.map((pane) => (
            <TabsContent key={pane.value} value={pane.value}>
              {pane.content(task)}
            </TabsContent>
          ))}
          <TabsContent value="attempts">
            <BackgroundTaskAttempts task={task} />
          </TabsContent>
          <TabsContent value="activity">
            <BackgroundTaskActivity task={task} />
          </TabsContent>
        </Tabs>
      </TaskPage>
    </FullBleed>
  )
}
