import { CheckListIcon } from "@hugeicons/core-free-icons"
import { ActivityMark } from "./activity-mark"
import { Icon } from "@/components/forge/icon"
import { PlanSteps } from "./plan-steps"
import { PanelButton, SidePanel } from "./side-panel"
import { TurnDetails } from "./turn-details"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { activityLabels, type ActivityItem } from "./lib/activity"
import { finishedSteps, usePlan } from "./lib/plan"
import { useAgentActivity } from "./lib/use-agent-activity"

export function PlanPanel() {
  const plan = usePlan()
  const activity = useAgentActivity()
  const done = plan ? finishedSteps(plan) : 0
  const total = plan?.steps.length ?? 0
  const completed =
    plan?.steps.filter((step) => step.status === "done").length ?? 0
  const skipped =
    plan?.steps.filter((step) => step.status === "skipped").length ?? 0
  return (
    <SidePanel
      panel="plan"
      title="Task details"
      icon={CheckListIcon}
      className="w-80 xl:w-[352px]"
    >
      <Tabs
        key={activity.sessionId ?? "new"}
        defaultValue={plan ? "plan" : "activity"}
        className="min-h-0 flex-1 gap-0"
      >
        <div className="border-b px-4 pt-2">
          <TabsList variant="line" className="w-full justify-start gap-5">
            <TabsTrigger value="plan" className="flex-none px-0 pb-2 text-xs">
              Plan
              {total > 0 && (
                <span className="font-normal text-muted-foreground tabular-nums">
                  {done}/{total}
                </span>
              )}
            </TabsTrigger>
            <TabsTrigger
              value="activity"
              className="flex-none px-0 pb-2 text-xs"
            >
              Activity
              <span className="font-normal text-muted-foreground tabular-nums">
                {activity.items.length}
              </span>
            </TabsTrigger>
          </TabsList>
        </div>
        <TabsContent value="plan" className="min-h-0 overflow-y-auto p-4">
          {!plan ? (
            <EmptyDetail
              title="Room for a plan"
              description="For work with several steps, the assistant will lay out a plan here and update it as it goes."
            />
          ) : (
            <>
              <p className="mb-2 text-2xs font-medium tracking-wide text-muted-foreground uppercase">
                Latest plan
              </p>
              <h3 className="text-base leading-snug font-medium text-foreground">
                {plan.title}
              </h3>
              <div className="mt-3 mb-5 flex items-center gap-2 text-xs text-muted-foreground">
                <Icon icon={done === total ? "completed" : "task"} size={14} />
                <span>
                  {completed} completed
                  {skipped > 0 ? `, ${skipped} skipped` : ""}
                </span>
                <span className="ml-auto tabular-nums">
                  {done} of {total}
                </span>
              </div>
              <PlanSteps
                plan={plan}
                detailed
                live={activity.running && activity.hasPlanActivity}
              />
              {done < total && !activity.running && (
                <p className="mt-5 border-t pt-3 text-xs leading-relaxed text-muted-foreground">
                  {activity.state === "waiting"
                    ? "Answer the request in the conversation to continue."
                    : "This plan has unfinished steps. Send a message to continue or change direction."}
                </p>
              )}
            </>
          )}
        </TabsContent>
        <TabsContent value="activity" className="min-h-0 overflow-y-auto p-4">
          <div className="mb-5 flex items-center gap-2.5">
            <ActivityMark state={activity.state} />
            <div>
              <h3 className="text-sm font-medium">
                {activityLabels[activity.state]}
              </h3>
              <p className="mt-0.5 text-2xs text-muted-foreground">
                Latest response
              </p>
            </div>
          </div>
          {activity.items.length === 0 ? (
            <EmptyDetail
              title={
                activity.running
                  ? "The assistant is working"
                  : "No tool activity"
              }
              description={
                activity.running
                  ? "Tool actions will appear here as they happen."
                  : "This response did not use any tools."
              }
            />
          ) : (
            <ol className="divide-y divide-border/70">
              {activity.items.map((item) => (
                <ActivityRow key={item.id} item={item} />
              ))}
            </ol>
          )}
          {activity.turn && activity.turn.calls > 0 && (
            <section className="mt-6 border-t pt-4">
              <h3 className="mb-3 text-xs font-medium">Response statistics</h3>
              <TurnDetails turn={activity.turn} />
            </section>
          )}
          {activity.running && (
            <p className="mt-5 text-2xs leading-relaxed text-muted-foreground">
              Usage and cost appear after the response finishes.
            </p>
          )}
        </TabsContent>
      </Tabs>
    </SidePanel>
  )
}

function EmptyDetail({
  title,
  description,
}: {
  title: string
  description: string
}) {
  return (
    <div className="py-6">
      <Icon icon="list" size={22} className="mb-3 text-muted-foreground" />
      <h3 className="text-sm font-medium">{title}</h3>
      <p className="mt-2 max-w-[32ch] text-xs leading-relaxed text-muted-foreground">
        {description}
      </p>
    </div>
  )
}

function ActivityRow({ item }: { item: ActivityItem }) {
  return (
    <li className="py-1">
      <details className="group">
        <summary className="flex cursor-pointer list-none items-start gap-2.5 rounded-md py-2.5 text-xs [&::-webkit-details-marker]:hidden">
          <ActivityMark state={item.state} />
          <span className="min-w-0 flex-1">
            <span className="block leading-relaxed wrap-anywhere text-foreground">
              {item.label}
            </span>
            <span className="mt-0.5 block text-2xs text-muted-foreground">
              {activityLabels[item.state]}
            </span>
          </span>
          <Icon
            icon="right"
            size={13}
            className="mt-1 text-muted-foreground transition-transform group-open:rotate-90"
          />
        </summary>
        <div className="mb-3 space-y-3 rounded-md bg-muted/50 p-3">
          <p className="font-mono text-2xs wrap-anywhere text-muted-foreground">
            {item.tool.toolName}
          </p>
          <RawDetail label="Input" value={item.tool.args} />
          {item.tool.result !== undefined && (
            <RawDetail label="Result" value={item.tool.result} />
          )}
          {item.state === "waiting" && (
            <p className="text-xs text-muted-foreground">
              Respond to the request in the conversation.
            </p>
          )}
          {item.state === "stopped" && (
            <p className="text-xs text-muted-foreground">
              No result was received for this action.
            </p>
          )}
        </div>
      </details>
    </li>
  )
}

function RawDetail({ label, value }: { label: string; value: unknown }) {
  return (
    <div>
      <p className="mb-1 text-2xs font-medium text-muted-foreground">{label}</p>
      <pre className="max-h-52 overflow-auto font-mono text-2xs leading-relaxed wrap-anywhere whitespace-pre-wrap">
        {typeof value === "string" ? value : JSON.stringify(value, null, 2)}
      </pre>
    </div>
  )
}

export function PlanButton() {
  const plan = usePlan()
  const { hasMessages } = useAgentActivity()
  if (!plan && !hasMessages) return null
  return (
    <PanelButton
      panel="plan"
      label="Task details"
      icon={CheckListIcon}
      badge={plan ? `${finishedSteps(plan)}/${plan.steps.length}` : "Activity"}
    />
  )
}
