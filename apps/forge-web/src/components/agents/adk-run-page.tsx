import * as React from "react"
import { LeftToRightListNumberIcon } from "@hugeicons/core-free-icons"
import { cn } from "cn"

import {
  ApprovalPanel,
  HumanInputPanel,
} from "@/components/background-tasks/approval-panel"
import {
  BackgroundTaskDetail,
  type TaskPageSection,
  type TaskPane,
} from "@/components/background-tasks/background-task-detail"
import { TaskPaneBody } from "@/components/background-tasks/background-task-panes"
import { KindGlyph } from "@/components/builder/glyph"
import { PanelEmpty } from "@/components/forge/empty-state"
import { ErrorCallout } from "@/components/forge/feedback"
import { Icon } from "@/components/forge/icon"
import { Chip } from "@/components/forge/status"
import {
  Disclosure,
  ExecutionPlan,
  ExecutionStep,
} from "@/components/forge/task-page"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import type { MyOrganization } from "@/lib/access"
import { organizationAgents } from "@/lib/agents/api"
import type { AgentDocument } from "@/lib/agents/document"
import { AGENT_KINDS } from "@/lib/agents/model"
import {
  adkApprovePermission,
  pauseKindOf,
  responseSchemaOf,
  runDocumentOf,
  stepErrorOf,
  stepKindOf,
  stepNameOf,
  stepStatusOf,
  stepTiming,
  useAdkRunSteps,
  useAnswerAdkRun,
  useDecideAdkRun,
  type AdkRunStep,
  type AdkRunTab,
} from "@/lib/agents/runs"
import {
  failureTitle,
  humanize,
  type BackgroundTaskApproval,
  type BackgroundTaskDetail as Detail,
} from "@/lib/background-tasks"
import { WORKSPACE_VIEWS } from "@/lib/organization-workspace"

// An ADK workflow run's page, under ADK workflows.
const ADK_WORKFLOW_RUNS: TaskPageSection = {
  label: WORKSPACE_VIEWS.agents.label,
  icon: WORKSPACE_VIEWS.agents.icon,
  back: "All ADK workflow runs",
  missing: "ADK workflow run not found",
}

type Pause = {
  organizationId: string
  taskId: string
  approval: BackgroundTaskApproval
}

/** An approval node's question, decided through the run's session. */
function AdkApproval({ organizationId, taskId, approval }: Pause) {
  // The panel shows the failure itself.
  const decide = useDecideAdkRun(organizationId, taskId, {
    meta: { silent: true },
  })
  return (
    <ApprovalPanel
      organizationId={organizationId}
      approval={approval}
      decide={decide}
      permission={adkApprovePermission(
        approval.details.approvers ?? "org:admin"
      )}
      docs="ADK workflows"
      showDeadline
    />
  )
}

/** A human input node's question, answered through the run's session. */
function AdkHumanInput({ organizationId, taskId, approval }: Pause) {
  // The panel shows the failure itself, a 422's why by the answer.
  const answer = useAnswerAdkRun(organizationId, taskId, {
    meta: { silent: true },
  })
  return (
    <HumanInputPanel
      organizationId={organizationId}
      approval={approval}
      responseSchema={responseSchemaOf(approval)}
      answer={answer}
      permission="agents:run"
    />
  )
}

/** What a paused run waits for: a decision, or a person's answer. */
function AdkPausePanel(pause: Pause) {
  return pauseKindOf(pause.approval) === "human_input" ? (
    <AdkHumanInput {...pause} />
  ) : (
    <AdkApproval {...pause} />
  )
}

/** What a step handed on, or why it failed: text as it is, else JSON. */
function StepValue({
  summary,
  value,
  defaultOpen,
}: {
  summary: string
  value: unknown
  defaultOpen?: boolean
}) {
  return (
    <Disclosure summary={summary} defaultOpen={defaultOpen} className="mt-1">
      <pre>
        {typeof value === "string" ? value : JSON.stringify(value, null, 2)}
      </pre>
    </Disclosure>
  )
}

function Step({
  step,
  index,
  doc,
}: {
  step: AdkRunStep
  index: number
  doc: AgentDocument | undefined
}) {
  const kind = stepKindOf(step, doc)
  const info = kind && AGENT_KINDS[kind]
  const shown = stepStatusOf(step.status)
  const hasOutput = step.output !== null && step.output !== undefined
  const error = stepErrorOf(step.error)
  // A step that took its Error way is done, and hands on its error.
  const failed = step.status === "failed"
  return (
    <ExecutionStep
      step={String(index + 1).padStart(2, "0")}
      completed={step.status === "done"}
      name={
        <span className="inline-flex min-w-0 items-center gap-2">
          {info ? (
            <KindGlyph info={info} size="sm" />
          ) : (
            <span
              aria-hidden="true"
              className="grid size-6 shrink-0 place-items-center rounded-(--radius-soft) border bg-background text-muted-foreground"
            >
              <Icon icon="layers" size={13} />
            </span>
          )}
          <span className="wrap-anywhere">{stepNameOf(step, doc)}</span>
        </span>
      }
      role={info?.label ?? humanize(step.kind || "step")}
      status={<Chip tone={shown.tone}>{shown.label}</Chip>}
      detail={stepTiming(step)}
    >
      {error && (
        <p
          className={cn(
            "flex items-start gap-1.5 text-sm/[1.6] wrap-anywhere",
            failed ? "text-destructive" : "text-muted-foreground"
          )}
        >
          <Icon
            icon={failed ? "failed" : "warning"}
            size={14}
            className="mt-[0.2rem] shrink-0"
          />
          <span>
            {failed ? error.message : `Took its Error way: ${error.message}`}
            {error.code && (
              <code className="ml-1.5 text-xs text-muted-foreground">
                {error.code}
              </code>
            )}
          </span>
        </p>
      )}
      {error?.more !== undefined && error.more !== null && (
        <StepValue summary="Error details" value={error.more} />
      )}
      {hasOutput && (
        <StepValue
          summary="Output"
          value={step.output}
          // An End's output is the run's result.
          defaultOpen={kind === "end"}
        />
      )}
    </ExecutionStep>
  )
}

/**
 * The Steps tab: every node of the ADK workflow, in order, as the run went
 * through it (done, failed, waiting or not reached), with what each handed
 * on or why it failed, and when. What the run waits for is answered above
 * them, as on Overview. The nodes' kinds come from the ADK workflow the run
 * started with (else as it's saved now, else as the run says).
 */
function AdkRunSteps({
  organizationId,
  task,
}: {
  organizationId: string
  task: Detail
}) {
  const steps = useAdkRunSteps(organizationId, task.id)
  const snapshot = runDocumentOf(task.payload)
  const saved = organizationAgents
    .scope({ organizationId })
    .useDetail(snapshot ? undefined : task.labels?.adk_workflow || undefined)
  const doc = snapshot ?? saved.data?.document
  const list = steps.data?.steps

  let body: React.ReactNode
  if (steps.error && !steps.data) {
    body =
      steps.error.status === 404 ? (
        <PanelEmpty illustration="search">
          It isn&apos;t one of the organization&apos;s ADK workflow runs, so it
          has no steps to show.
        </PanelEmpty>
      ) : (
        <ErrorCallout
          title={failureTitle(steps.error, "Couldn't load the run's steps")}
          action={
            <Button
              variant="outline"
              size="sm"
              onClick={() => void steps.refetch()}
            >
              Retry
            </Button>
          }
        >
          {steps.error.message}
        </ErrorCallout>
      )
  } else if (!list) {
    body = (
      <div className="flex flex-col gap-3" aria-busy="true">
        <Skeleton className="h-12 w-full" />
        <Skeleton className="h-12 w-full" />
        <Skeleton className="h-12 w-full" />
      </div>
    )
  } else if (list.length === 0) {
    body = (
      <PanelEmpty illustration="waiting">
        No step has run yet. The steps show here once a worker starts the run.
      </PanelEmpty>
    )
  } else {
    body = (
      <>
        <p className="mb-2 max-w-[75ch] text-[0.8125rem] text-muted-foreground">
          Every node of the ADK workflow, in order, and how far the run got with
          each: what it handed on (in a loop, its last item&apos;s), or why it
          failed. What runs inside a node, a team&apos;s sub-agents or a saved
          workflow&apos;s steps, counts as that node.
        </p>
        <ExecutionPlan aria-label="Steps" className="max-w-[75ch]">
          {list.map((step, index) => (
            <Step
              key={`${step.id}-${index}`}
              step={step}
              index={index}
              doc={doc}
            />
          ))}
        </ExecutionPlan>
      </>
    )
  }

  return (
    <TaskPaneBody>
      {task.approval && task.status === "AWAITING_VALIDATION" && (
        <AdkPausePanel
          organizationId={organizationId}
          taskId={task.id}
          approval={task.approval}
        />
      )}
      {body}
    </TaskPaneBody>
  )
}

/**
 * One of the organization's ADK workflow runs, under ADK workflows: a
 * background task's page (its header, actions, Overview, Attempts and
 * Activity) with a Steps tab, and what it waits for answered in Overview
 * and Steps: an approval node's decision, or a human input node's answer.
 */
export function AdkRunPage({
  organization,
  taskId,
  tab,
  canManage,
  onTabChange,
  onBack,
}: {
  organization: MyOrganization
  taskId: string
  tab: AdkRunTab
  /** `background_tasks:manage` in the organization: offer the actions. */
  canManage: boolean
  onTabChange: (tab: AdkRunTab) => void
  onBack: () => void
}) {
  const panes: TaskPane<"steps">[] = [
    {
      value: "steps",
      label: "Steps",
      icon: LeftToRightListNumberIcon,
      live: true,
      content: (task) => (
        <AdkRunSteps organizationId={organization.id} task={task} />
      ),
    },
  ]
  return (
    <BackgroundTaskDetail<"steps">
      organization={organization}
      taskId={taskId}
      tab={tab}
      canManage={canManage}
      section={ADK_WORKFLOW_RUNS}
      panes={panes}
      approvalPanel={(task) => (
        <AdkPausePanel
          organizationId={organization.id}
          taskId={task.id}
          approval={task.approval}
        />
      )}
      onTabChange={onTabChange}
      onBack={onBack}
    />
  )
}
