import * as React from "react"
import { cn } from "cn"

import { PageEmpty, PanelEmpty } from "@/components/forge/empty-state"
import { ErrorCallout } from "@/components/forge/feedback"
import { Icon } from "@/components/forge/icon"
import { Chip } from "@/components/forge/status"
import {
  Disclosure,
  RecordList,
  RecordRow,
  TaskMeta,
  TaskPage,
  TaskPageEyebrow,
  TaskPageHeader,
  TaskTabBar,
  TaskTabsList,
  TaskTabsTrigger,
  TaskTitleRow,
} from "@/components/forge/task-page"
import { CopyButton } from "@/features/json/components/json-view"
import { ValueTree } from "@/features/json/components/value-tree"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { Tabs, TabsContent } from "@/components/ui/tabs"
import { downloadBlob } from "@/features/builder/components/utils"
import {
  ChannelChips,
  Who,
} from "@/features/conversations/components/conversation-list"
import { ConversationReplay } from "@/features/conversations/components/conversation-replay"
import {
  ASSISTANT_APP,
  callTotals,
  formatCost,
  formatTokens,
  useConversation,
  whoIs,
  type AuditScope,
  type ConversationDetail,
  type ConversationRef,
} from "@/features/conversations/lib/conversations"
import {
  TraceStateView,
  TraceTimeline,
} from "@/features/runs/components/run-trace"
import type { AdkRunTrace, TraceEvent } from "@/features/runs/lib/trace"
import { formatDateTime, formatDuration, formatRelative } from "@/lib/format"

const BODY =
  "w-full max-w-[1500px] px-[30px] pt-7 pb-[60px] @max-[900px]/shell:px-5 @max-[900px]/shell:py-6 @max-[600px]/shell:px-[15px] @max-[600px]/shell:py-5"

type Tab = "conversation" | "tools" | "usage" | "state" | "events"
const TABS: Tab[] = ["conversation", "tools", "usage", "state", "events"]

// ADK's own calls: a question for a person, a tool waiting to be allowed.
const PAUSE_CALLS = new Set(["adk_request_input", "adk_request_confirmation"])

/* -------------------------------------------------------------------------- */
/* The conversation                                                           */
/* -------------------------------------------------------------------------- */

function ToolCard({
  event,
  call,
  answer,
  onInspect,
}: {
  event: TraceEvent
  call: TraceEvent["calls"][number]
  answer: { event: TraceEvent; response: unknown } | undefined
  onInspect: (seq: number) => void
}) {
  const failed =
    answer !== undefined &&
    typeof answer.response === "object" &&
    answer.response !== null &&
    ("error" in answer.response ||
      ["error", "failed"].includes(
        String((answer.response as Record<string, unknown>).status)
      ))
  return (
    <div className="flex w-[min(48rem,92%)] flex-col gap-2 self-start rounded-(--radius-card) border border-dashed px-3 py-2">
      <div className="flex min-w-0 flex-wrap items-center gap-2">
        <Icon
          icon="code"
          size={13}
          className="shrink-0 text-muted-foreground"
        />
        <code className="text-xs text-foreground">{call.name}</code>
        <span className="text-3xs text-muted-foreground">
          by {event.author}
        </span>
        <Chip
          tone={!answer ? "warning" : failed ? "danger" : "success"}
          className="h-5 px-1.5 text-2xs"
        >
          {!answer ? "No answer" : failed ? "Failed" : "Answered"}
        </Chip>
        <Button
          variant="link"
          size="xs"
          className="ml-auto h-auto p-0"
          onClick={() => onInspect(event.seq)}
        >
          Inspect #{event.seq}
        </Button>
      </div>
      <Disclosure summary="Sent">
        <ValueTree
          value={call.args}
          openDepth={2}
          className="border-0 bg-transparent p-0"
        />
      </Disclosure>
      {answer && (
        <Disclosure summary="Answered">
          <ValueTree
            value={answer.response}
            openDepth={2}
            className="border-0 bg-transparent p-0"
          />
        </Disclosure>
      )}
    </div>
  )
}

/* -------------------------------------------------------------------------- */
/* Tools and usage                                                            */
/* -------------------------------------------------------------------------- */

function Tools({
  detail,
  onInspect,
}: {
  detail: ConversationDetail
  onInspect: (seq: number) => void
}) {
  const rows = React.useMemo(() => {
    const answers = new Map<string, { event: TraceEvent; response: unknown }>()
    for (const event of detail.events)
      for (const response of event.responses)
        if (response.id)
          answers.set(response.id, { event, response: response.response })
    return detail.events.flatMap((event) =>
      event.calls
        .filter((call) => !PAUSE_CALLS.has(call.name ?? ""))
        .map((call) => ({
          event,
          call,
          answer: call.id ? answers.get(call.id) : undefined,
        }))
    )
  }, [detail.events])
  if (!rows.length)
    return (
      <PanelEmpty illustration="activity">
        No tool was called in this conversation.
      </PanelEmpty>
    )
  return (
    <div className="flex max-w-[60rem] flex-col gap-3">
      {rows.map(({ event, call, answer }) => (
        <div
          key={`${event.seq}-${call.id ?? call.name}`}
          className="flex flex-col gap-1"
        >
          <span className="text-3xs text-muted-foreground">
            {formatDateTime(event.at)}
          </span>
          <ToolCard
            event={event}
            call={call}
            answer={answer}
            onInspect={onInspect}
          />
        </div>
      ))}
    </div>
  )
}

function Usage({ detail }: { detail: ConversationDetail }) {
  const totals = callTotals(detail.calls)
  const facts: [string, React.ReactNode][] = [
    ["Turns", detail.turns.length.toLocaleString()],
    ["Model calls", totals.modelCalls.toLocaleString()],
    ["Tool calls", totals.toolCalls.toLocaleString()],
    ["Input tokens", totals.inputTokens.toLocaleString()],
    ["Output tokens", totals.outputTokens.toLocaleString()],
    ["Cached tokens", totals.cachedTokens.toLocaleString()],
    ["Thinking tokens", totals.thinkingTokens.toLocaleString()],
    ["Cost", formatCost(totals.cost)],
  ]
  return (
    <div className="flex flex-col gap-8">
      <dl className="grid grid-cols-4 rounded-(--radius-card) border @max-[800px]/shell:grid-cols-2">
        {facts.map(([label, value]) => (
          <div
            key={label}
            className="border-r border-b px-3.5 py-2.5 nth-[4n]:border-r-0"
          >
            <dt className="text-2xs text-muted-foreground">{label}</dt>
            <dd className="mt-1 text-sm tabular-nums">{value}</dd>
          </div>
        ))}
      </dl>
      <section>
        <h2 className="mb-3 text-lg font-[550]">Turns</h2>
        {detail.turns.length === 0 ? (
          <p className="text-xs text-muted-foreground">
            No turn of it was recorded: it's from before turns were kept.
          </p>
        ) : (
          <div className="overflow-x-auto rounded-(--radius-card) border">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Started</TableHead>
                  <TableHead>Channel</TableHead>
                  <TableHead>Called by</TableHead>
                  <TableHead>Version</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead className="text-right">Took</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {detail.turns.map((turn) => (
                  <TableRow key={turn.id}>
                    <TableCell className="whitespace-nowrap">
                      {turn.started_at ? formatDateTime(turn.started_at) : "—"}
                    </TableCell>
                    <TableCell>
                      <ChannelChips channels={[turn.channel ?? "unknown"]} />
                    </TableCell>
                    <TableCell className="max-w-56">
                      <Who id={turn.caller} people={detail.people} />
                    </TableCell>
                    <TableCell>{turn.version ?? "—"}</TableCell>
                    <TableCell>
                      <Chip
                        tone={
                          turn.status === "succeeded"
                            ? "success"
                            : turn.status === "failed"
                              ? "danger"
                              : "warning"
                        }
                        className="h-5 px-1.5 text-2xs"
                      >
                        {turn.status}
                      </Chip>
                      {turn.error && (
                        <span
                          className="mt-1 block max-w-80 truncate text-3xs text-destructive"
                          title={turn.error}
                        >
                          {turn.error}
                        </span>
                      )}
                    </TableCell>
                    <TableCell className="text-right tabular-nums">
                      {turn.duration_ms === null
                        ? "—"
                        : formatDuration(turn.duration_ms)}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        )}
      </section>
      <section>
        <h2 className="mb-3 text-lg font-[550]">Model and tool calls</h2>
        {detail.calls.length === 0 ? (
          <p className="text-xs text-muted-foreground">
            No call of it was recorded.
          </p>
        ) : (
          <div className="overflow-x-auto rounded-(--radius-card) border">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>When</TableHead>
                  <TableHead>Agent</TableHead>
                  <TableHead>Model or tool</TableHead>
                  <TableHead className="text-right">In</TableHead>
                  <TableHead className="text-right">Out</TableHead>
                  <TableHead className="text-right">Cached</TableHead>
                  <TableHead className="text-right">Thinking</TableHead>
                  <TableHead className="text-right">Cost</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {detail.calls.map((call) => (
                  <TableRow
                    key={call.id}
                    className={cn(call.failed && "text-destructive")}
                  >
                    <TableCell className="whitespace-nowrap">
                      {call.at ? formatDateTime(call.at) : "—"}
                    </TableCell>
                    <TableCell>{call.author}</TableCell>
                    <TableCell>
                      <span className="flex items-center gap-1.5">
                        <Icon
                          icon={call.call === "model" ? "sparkles" : "code"}
                          size={12}
                          className="shrink-0 text-muted-foreground"
                        />
                        <code className="text-xs">{call.name}</code>
                        {call.failed && (
                          <Chip tone="danger" className="h-5 px-1.5 text-2xs">
                            Failed
                          </Chip>
                        )}
                      </span>
                    </TableCell>
                    {call.call === "model" ? (
                      <>
                        <TableCell className="text-right tabular-nums">
                          {call.input_tokens.toLocaleString()}
                        </TableCell>
                        <TableCell className="text-right tabular-nums">
                          {call.output_tokens.toLocaleString()}
                        </TableCell>
                        <TableCell className="text-right tabular-nums">
                          {call.cached_tokens.toLocaleString()}
                        </TableCell>
                        <TableCell className="text-right tabular-nums">
                          {call.thinking_tokens.toLocaleString()}
                        </TableCell>
                        <TableCell className="text-right tabular-nums">
                          {formatCost(call.cost)}
                        </TableCell>
                      </>
                    ) : (
                      <TableCell
                        colSpan={5}
                        className="text-right text-muted-foreground"
                      >
                        Tool call
                      </TableCell>
                    )}
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        )}
      </section>
    </div>
  )
}

/* -------------------------------------------------------------------------- */
/* The page                                                                   */
/* -------------------------------------------------------------------------- */

/**
 * One conversation, whole, for its audit: what was said, turn by turn (each
 * one's channel, caller, time and tokens), the tools called and what they
 * answered, its usage, its state and every change to it, and every event to
 * inspect.
 */
export function ConversationPage({
  scope,
  conversation,
  onBack,
}: {
  scope: AuditScope
  conversation: ConversationRef
  onBack: () => void
}) {
  const found = useConversation(scope, conversation)
  const detail = found.data
  const [tab, setTab] = React.useState<Tab>("conversation")
  const [selected, setSelected] = React.useState<number>()
  const [step, setStep] = React.useState<string>()
  const trace = React.useMemo(
    () =>
      detail &&
      ({
        ...detail,
        invocation_id: null,
        earlier_sessions: [],
        worker: {},
      } as AdkRunTrace),
    [detail]
  )
  const inspect = (seq: number) => {
    setSelected(seq)
    setTab("events")
  }

  if (!detail || !trace) {
    if (found.error?.status === 404)
      return (
        <PageEmpty
          illustration="search"
          title="Conversation not found"
          description="It isn't one you may audit, or it no longer exists."
        >
          <Button variant="outline" size="sm" onClick={onBack}>
            <Icon icon="left" data-icon="inline-start" />
            All conversations
          </Button>
        </PageEmpty>
      )
    if (found.error)
      return (
        <ErrorCallout
          title="Couldn't load the conversation"
          action={
            <Button
              variant="outline"
              size="sm"
              onClick={() => void found.refetch()}
            >
              Retry
            </Button>
          }
        >
          {found.error.message}
        </ErrorCallout>
      )
    return (
      <div className="flex flex-col gap-3" aria-busy="true">
        <Skeleton className="h-8 w-64" />
        <Skeleton className="h-40 w-full" />
      </div>
    )
  }

  const totals = callTotals(detail.calls)
  const channels = [
    ...new Set(detail.turns.map((turn) => turn.channel ?? "unknown")),
  ]
  const exported = () =>
    downloadBlob(
      new Blob([JSON.stringify(detail, null, 2)], { type: "application/json" }),
      `conversation-${detail.session_id.slice(0, 8)}.json`
    )

  return (
    <div className="absolute inset-0 flex flex-col">
      <TaskPage>
        <TaskPageHeader>
          <TaskPageEyebrow
            onBack={onBack}
            backLabel="All conversations"
            reference={detail.session_id.slice(0, 8)}
          />
          <TaskTitleRow
            title={detail.agent_name}
            meta={
              <>
                <TaskMeta
                  icon={
                    detail.app_name === ASSISTANT_APP ? "sparkles" : "robot"
                  }
                >
                  {whoIs(detail.user_id, detail.people).label}
                </TaskMeta>
                <ChannelChips
                  channels={channels.length ? channels : ["unknown"]}
                />
                <TaskMeta icon="message">
                  {detail.turns.length === 1
                    ? "1 turn"
                    : `${detail.turns.length} turns`}
                </TaskMeta>
                <TaskMeta icon="coins">
                  {formatTokens(totals.inputTokens + totals.outputTokens)}{" "}
                  tokens · {formatCost(totals.cost)}
                </TaskMeta>
                {detail.updated_at && (
                  <TaskMeta icon="clock">
                    Active {formatRelative(detail.updated_at)}
                  </TaskMeta>
                )}
              </>
            }
            actions={
              <Button variant="outline" size="sm" onClick={exported}>
                <Icon icon="download" data-icon="inline-start" />
                Export JSON
              </Button>
            }
          />
        </TaskPageHeader>
        <Tabs
          value={tab}
          onValueChange={(value) => {
            if (TABS.includes(value as Tab)) setTab(value as Tab)
          }}
          className="gap-0"
        >
          <TaskTabBar>
            <TaskTabsList aria-label="Conversation pages">
              <TaskTabsTrigger value="conversation" icon="message">
                Conversation
              </TaskTabsTrigger>
              <TaskTabsTrigger value="tools" icon="code">
                Tools
              </TaskTabsTrigger>
              <TaskTabsTrigger value="usage" icon="coins">
                Usage
              </TaskTabsTrigger>
              <TaskTabsTrigger value="state" icon="layers">
                State
              </TaskTabsTrigger>
              <TaskTabsTrigger value="events" icon="activity">
                Events
              </TaskTabsTrigger>
            </TaskTabsList>
          </TaskTabBar>
          <div className={BODY}>
            {detail.partial && (
              <p
                role="note"
                className="mb-5 flex max-w-[75ch] items-center gap-3 rounded-(--radius-card) border bg-notice-surface px-3 py-2 text-xs text-notice-foreground"
              >
                <Icon
                  icon="info"
                  size={14}
                  className="shrink-0 text-notice-accent"
                />
                Only the turns asked from inside this organization are shown:
                the person may have asked the assistant elsewhere in the same
                conversation.
              </p>
            )}
            {detail.omitted > 0 && (
              <p
                role="note"
                className="mb-5 flex max-w-[75ch] items-center gap-3 rounded-(--radius-card) border bg-notice-surface px-3 py-2 text-xs text-notice-foreground"
              >
                <Icon
                  icon="info"
                  size={14}
                  className="shrink-0 text-notice-accent"
                />
                Its first {detail.omitted.toLocaleString()} events aren't
                listed: the audit lists its latest. Export it for the record.
              </p>
            )}
            <TabsContent value="conversation">
              <ConversationReplay detail={detail} />
            </TabsContent>
            <TabsContent value="tools">
              <Tools detail={detail} onInspect={inspect} />
            </TabsContent>
            <TabsContent value="usage">
              <Usage detail={detail} />
            </TabsContent>
            <TabsContent value="state">
              <TraceStateView
                trace={trace}
                doc={undefined}
                subject="conversation"
                onOpenEvent={inspect}
              />
            </TabsContent>
            <TabsContent value="events">
              {detail.events.length === 0 ? (
                <PanelEmpty illustration="activity">
                  No event to show.
                </PanelEmpty>
              ) : (
                <TraceTimeline
                  trace={trace}
                  doc={undefined}
                  step={step}
                  onStepChange={setStep}
                  selected={selected}
                  onSelect={setSelected}
                  subject="conversation"
                />
              )}
            </TabsContent>
            <section className="mt-10 max-w-[60rem]">
              <h2 className="mb-3 text-lg font-[550]">Record</h2>
              <RecordList>
                <RecordRow label="Agent">
                  {detail.agent_name}{" "}
                  <code className="text-muted-foreground">
                    {detail.app_name}
                  </code>
                </RecordRow>
                <RecordRow label="User">
                  <Who id={detail.user_id} people={detail.people} />
                </RecordRow>
                <RecordRow label="ADK session">
                  <span className="flex items-center gap-2">
                    <code>{detail.session_id}</code>
                    <CopyButton
                      value={detail.session_id}
                      label="Copy"
                      size="xs"
                    />
                  </span>
                </RecordRow>
                {detail.created_at && (
                  <RecordRow label="Started">
                    {formatDateTime(detail.created_at)}
                  </RecordRow>
                )}
                {detail.updated_at && (
                  <RecordRow label="Last active">
                    {formatDateTime(detail.updated_at)}
                  </RecordRow>
                )}
              </RecordList>
            </section>
          </div>
        </Tabs>
      </TaskPage>
    </div>
  )
}
