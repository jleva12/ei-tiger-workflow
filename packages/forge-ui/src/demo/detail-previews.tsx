import * as React from "react"
import { cn } from "cn"

import { Button } from "@/components/ui/button"
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Tabs, TabsContent } from "@/components/ui/tabs"
import {
  ChangedFile,
  DiffTotals,
  DiffView,
  DiffWorkspace,
  FileDiff,
  FileDiffHeader,
  FileFilter,
  FileList,
  FileListLabel,
  FileNavigator,
} from "@/components/forge/changes"
import {
  CommandOutput,
  Finding,
  HandoffAgent,
  HandoffIteration,
  HandoffResult,
  VerificationCommands,
  VerificationRound,
} from "@/components/forge/handoff"
import { Icon } from "@/components/forge/icon"
import {
  MetricsBody,
  MetricsFooter,
  MetricsHeader,
  MetricsRow,
  MetricsSummary,
  MetricsTable,
  MetricValue,
  Unreported,
} from "@/components/forge/metrics-table"
import {
  JobButton,
  JobsLabel,
  JobsSidebar,
  LogConsole,
  LogEmpty,
  LogEntry,
  LogFooter,
  LogPane,
  LogPaneHeading,
  LogScroll,
  LogSearch,
  LogToolbar,
  LogToolbarButton,
  LogToolbarText,
  RunLogLayout,
  RunStat,
  RunStats,
} from "@/components/forge/run-log"
import { Chip, StatusBadge } from "@/components/forge/status"
import {
  AsideFact,
  AsideSection,
  Disclosure,
  ExecutionPlan,
  ExecutionStep,
  LiveLabel,
  PageSection,
  Prose,
  RecordList,
  RecordRow,
  SubmissionLayout,
  TaskMeta,
  TaskPage,
  TaskPageEyebrow,
  TaskPageHeader,
  TaskTabBar,
  TaskTabsList,
  TaskTabsTrigger,
  TaskTitleRow,
  WaitingNotice,
} from "@/components/forge/task-page"
import type { TaskItem } from "@/components/forge/task-list"
import { diffLines, logEntries } from "./data"

/* -------------------------------------------------------------------------- */
/* Header + tabs                                                              */
/* -------------------------------------------------------------------------- */

export function TaskHeaderPreview({
  task,
  onBack,
}: {
  task: Pick<TaskItem, "id" | "title" | "status" | "repository">
  onBack?: () => void
}) {
  return (
    <TaskPageHeader>
      <TaskPageEyebrow
        onBack={onBack}
        reference={task.id}
        label="Read-only example"
      />
      <TaskTitleRow
        title={task.title}
        meta={
          <>
            <StatusBadge status={task.status} size="lg" />
            <TaskMeta icon="folder">{task.repository}</TaskMeta>
            <TaskMeta icon="branch">main</TaskMeta>
            <span>Run #3</span>
          </>
        }
        actions={
          <>
            <Button variant="outline" size="sm">
              <Icon icon="link" data-icon="inline-start" />
              Copy link
            </Button>
            <Button variant="outline" size="sm" disabled>
              <Icon icon="review" data-icon="inline-start" />
              Open PR
            </Button>
          </>
        }
      />
    </TaskPageHeader>
  )
}

const runs = [
  { value: "latest", label: "Latest run · #3" },
  { value: "2", label: "Run #2 · failed" },
  { value: "1", label: "Run #1 · cancelled" },
]

export function RunSelector() {
  return (
    <div className="flex items-center gap-[13px] py-2">
      <Select items={runs} defaultValue="latest">
        <SelectTrigger
          size="sm"
          aria-label="Task run"
          className="max-w-[200px] rounded-(--radius-soft) text-[0.8125rem]"
        >
          <SelectValue />
        </SelectTrigger>
        <SelectContent alignItemWithTrigger={false} align="start">
          <SelectGroup>
            {runs.map((run) => (
              <SelectItem key={run.value} value={run.value}>
                {run.label}
              </SelectItem>
            ))}
          </SelectGroup>
        </SelectContent>
      </Select>
      <LiveLabel online={false}>Preview</LiveLabel>
    </div>
  )
}

export function TaskDetailPreview({
  task,
  onBack,
  defaultTab = "submission",
}: {
  task: Pick<TaskItem, "id" | "title" | "status" | "repository">
  onBack?: () => void
  defaultTab?: string
}) {
  return (
    <TaskPage>
      <TaskHeaderPreview task={task} onBack={onBack} />
      <Tabs defaultValue={defaultTab} className="gap-0">
        <TaskTabBar>
          <TaskTabsList aria-label="Task run pages">
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
          <RunSelector />
        </TaskTabBar>
        <TabsContent value="submission">
          <SubmissionPreview />
        </TabsContent>
        <TabsContent value="logs">
          <RunLogPreview />
        </TabsContent>
        <TabsContent value="changes">
          <ChangesPreview className="h-[640px]" />
        </TabsContent>
        <TabsContent value="handoff">
          <HandoffPreview />
        </TabsContent>
        <TabsContent value="usage">
          <UsagePreview />
        </TabsContent>
      </Tabs>
    </TaskPage>
  )
}

/* -------------------------------------------------------------------------- */
/* Submission                                                                 */
/* -------------------------------------------------------------------------- */

export function SubmissionPreview({ waiting = false }: { waiting?: boolean }) {
  return (
    <SubmissionLayout
      aside={
        <>
          <AsideSection title="Run configuration">
            <AsideFact label="Repository">
              https://github.com/workspace/coding-agent.git
            </AsideFact>
            <AsideFact label="Base branch">
              <Icon icon="branch" size={14} />
              main
            </AsideFact>
            <AsideFact label="Maximum fix attempts">3</AsideFact>
            <AsideFact label="Time limit">30 minutes</AsideFact>
            <AsideFact label="Verification policy">
              blocking · not run is advisory · 15 min per command
            </AsideFact>
          </AsideSection>
          <AsideSection title="Queue & dispatch">
            <AsideFact label="Queue">application-tasks</AsideFact>
            <AsideFact label="Attempts">1 of 3</AsideFact>
          </AsideSection>
        </>
      }
    >
      {waiting && (
        <WaitingNotice title="Waiting for a worker" state="Queued">
          The run is queued. Its execution plan is fixed and will start as soon
          as a worker picks it up.
        </WaitingNotice>
      )}
      <PageSection title="Task brief">
        <Prose>
          {`Test worker reconnection

Implement the requested change, cover edge cases with tests, and have the configured reviewer check the changes before committing.`}
        </Prose>
      </PageSection>
      <PageSection
        title="Execution plan"
        meta="3 configured agents"
        description="Implementation runs first. The worker then verifies the tree itself using the repository's .forge.md steps and the coder's reported commands, followed by parallel reviews. The fixer runs if a blocking review requests changes."
      >
        <ExecutionPlan>
          <ExecutionStep
            step="01"
            completed
            name="Implementer"
            role="implementer"
            status="completed"
            detail="example / preview-model"
          >
            <Disclosure summary="Agent instructions">
              <pre>
                {`Implement the task on the dedicated branch.
Run the repository's tests and report every command you executed.`}
              </pre>
            </Disclosure>
          </ExecutionStep>
          <ExecutionStep
            step="02"
            name="Code reviewer"
            role="reviewer"
            status="running"
            detail="example / preview-model"
          >
            <Disclosure summary="Agent instructions">
              <pre>Review the pinned diff for correctness and coverage.</pre>
            </Disclosure>
          </ExecutionStep>
          <ExecutionStep
            step="03"
            name="Fixer"
            role="fixer"
            status="runs if changes requested"
            detail="example / preview-model"
          />
        </ExecutionPlan>
      </PageSection>
      <PageSection title="Submission record">
        <RecordList>
          <RecordRow label="Task ID">
            <code>FG-214</code>
          </RecordRow>
          <RecordRow label="Submitted">Sep 8, 2026, 9:01 AM</RecordRow>
          <RecordRow label="Idempotency key">
            <code>preview-10</code>
          </RecordRow>
          <RecordRow label="Plan">
            Default coding workflow · revision 4
          </RecordRow>
        </RecordList>
      </PageSection>
    </SubmissionLayout>
  )
}

/* -------------------------------------------------------------------------- */
/* Run logs                                                                   */
/* -------------------------------------------------------------------------- */

export function RunLogPreview() {
  const [job, setJob] = React.useState("all")
  const [follow, setFollow] = React.useState(true)
  const [query, setQuery] = React.useState("")
  const shown = logEntries.filter((entry) =>
    `${entry.title} ${entry.text ?? ""}`
      .toLowerCase()
      .includes(query.trim().toLowerCase())
  )
  return (
    <RunLogLayout>
      <JobsSidebar>
        <JobsLabel trailing="#3">Workflow</JobsLabel>
        <JobButton
          icon="activity"
          name="All events"
          count={logEntries.length}
          active={job === "all"}
          onClick={() => setJob("all")}
        />
        <JobsLabel>Agent jobs</JobsLabel>
        <JobButton
          state="completed"
          name="Implementer"
          details={["Iteration 1 · Attempt 1", "Reasoning: medium"]}
          active={job === "coder"}
          onClick={() => setJob("coder")}
        />
        <JobButton
          state="completed"
          name="Code reviewer"
          details={["Iteration 1 · Attempt 1", "Reasoning: medium"]}
          active={job === "review-1"}
          onClick={() => setJob("review-1")}
        />
        <JobButton
          state="running"
          name="Code reviewer"
          details={["Iteration 2 · Attempt 1", "Reasoning: medium"]}
          active={job === "review-2"}
          onClick={() => setJob("review-2")}
        />
        <JobButton
          state="planned"
          name="Fixer"
          details={["Runs if changes requested"]}
        />
        <RunStats>
          <RunStat label="Started">09:02 AM</RunStat>
          <RunStat label="Duration">3m 42s</RunStat>
          <RunStat label="Usage">115,600 tokens</RunStat>
          <RunStat label="Cost">$0.4212</RunStat>
        </RunStats>
      </JobsSidebar>
      <LogPane>
        <LogPaneHeading
          title="Workflow activity"
          state="running"
          subtitle="Code reviewer is working"
          actions={
            <Button variant="outline" size="sm">
              <Icon icon="download" data-icon="inline-start" />
              {query ? "Export matches" : "Download logs"}
            </Button>
          }
        />
        <LogConsole>
          <LogToolbar>
            <LogSearch
              value={query}
              onChange={(event) => {
                setQuery(event.target.value)
                setFollow(false)
              }}
            />
            <LogToolbarText>
              {query ? `${shown.length} matches` : `${shown.length} events`}
            </LogToolbarText>
            <LogToolbarButton
              aria-pressed={follow}
              onClick={() => setFollow((value) => !value)}
            >
              <Icon icon="down" data-icon="inline-start" />
              {follow ? "Following" : "Follow logs"}
            </LogToolbarButton>
          </LogToolbar>
          <LogScroll className="h-[380px] min-h-0">
            {shown.map((entry, index) => (
              <LogEntry
                key={entry.title}
                number={index + 1}
                kind={entry.kind}
                status={entry.status}
                title={entry.title}
                time={entry.time}
                defaultOpen={entry.open || query.length > 0}
              >
                {entry.text}
              </LogEntry>
            ))}
            {!shown.length && (
              <LogEmpty illustration="search" title="No matching log entries">
                Try a job name, a file path or part of an error message.
              </LogEmpty>
            )}
          </LogScroll>
          <LogFooter online={false} trailing="Updates automatically">
            Example output · no live execution
          </LogFooter>
        </LogConsole>
      </LogPane>
    </RunLogLayout>
  )
}

/* -------------------------------------------------------------------------- */
/* Changes                                                                    */
/* -------------------------------------------------------------------------- */

const changedFiles = [
  { path: "src/middleware/auth.ts", added: 12, removed: 2 },
  { path: "tests/auth.test.ts", added: 11, removed: 0 },
  { path: "src/lib/errors.ts", added: 4, removed: 1 },
]

export function ChangesPreview({ className }: { className?: string }) {
  const [active, setActive] = React.useState(changedFiles[0].path)
  const [wrap, setWrap] = React.useState(false)
  const file = changedFiles.find((item) => item.path === active)!
  return (
    <div
      className={cn(
        "flex flex-col px-[30px] py-[25px] @max-[900px]/shell:px-5 @max-[600px]/shell:px-[15px]",
        className
      )}
    >
      <DiffWorkspace>
        <FileNavigator>
          <FileFilter />
          <FileListLabel count={changedFiles.length} />
          <FileList>
            {changedFiles.map((item) => (
              <ChangedFile
                key={item.path}
                path={item.path}
                added={item.added}
                removed={item.removed}
                active={item.path === active}
                onClick={() => setActive(item.path)}
              />
            ))}
          </FileList>
          <div className="mt-4 flex items-center justify-between border-t px-2 pt-4">
            <span className="text-xs text-muted-foreground">
              Run #3 snapshot
            </span>
            <DiffTotals label="Total" added={27} removed={3} />
          </div>
        </FileNavigator>
        <FileDiff>
          <FileDiffHeader
            path={file.path}
            added={file.added}
            removed={file.removed}
            actions={
              <>
                <Button variant="ghost" size="sm">
                  <Icon icon="sparkles" data-icon="inline-start" />
                  Explain
                </Button>
                <Button
                  variant="ghost"
                  size="sm"
                  aria-pressed={wrap}
                  onClick={() => setWrap((value) => !value)}
                >
                  {wrap ? "No wrap" : "Wrap"}
                </Button>
                <Button variant="ghost" size="sm">
                  Patch
                </Button>
              </>
            }
          />
          <DiffView lines={diffLines} wrap={wrap} />
        </FileDiff>
      </DiffWorkspace>
    </div>
  )
}

/* -------------------------------------------------------------------------- */
/* Handoff                                                                    */
/* -------------------------------------------------------------------------- */

export function HandoffPreview() {
  return (
    <div className="max-w-[1400px] px-[30px] pt-[25px] pb-[50px] @max-[900px]/shell:px-5 @max-[600px]/shell:px-[15px]">
      <div className="mb-[25px] flex items-center justify-between gap-4 @max-[900px]/shell:flex-col @max-[900px]/shell:items-start">
        <div>
          <h2 className="mb-2 text-lg font-[550] tracking-[-.3px]">
            Agent handoffs
          </h2>
          <p className="text-sm/[1.6] text-muted-foreground">
            Instructions, evidence, and review findings from each pass.
          </p>
        </div>
        <span className="text-sm whitespace-nowrap text-muted-foreground">
          2 iterations · Latest first
        </span>
      </div>
      <HandoffIteration
        title="Iteration 2"
        latest
        count="2 handoffs"
        summary={
          <>
            <span>1 completed · 1 running</span>
            <span>fixer · reviewer</span>
          </>
        }
      >
        <VerificationRound
          title="Harness verification"
          result="passed"
          meta="via .forge.md steps · blocking policy"
          note="Test output was reverted before review"
        >
          <VerificationCommands
            commands={[
              {
                result: "passed",
                command: "npm ci",
                source: "forge",
                exitCode: 0,
                duration: "5.9 s",
                details: <CommandOutput />,
              },
              {
                result: "passed",
                command: "npm test",
                source: "forge",
                exitCode: 0,
                duration: "1.2 s",
                details: <CommandOutput />,
              },
              {
                result: "unable_to_verify",
                command: "docker compose config --quiet",
                source: "agent",
                duration: "0 ms",
                details: (
                  <CommandOutput
                    summary="Environment limitation"
                    defaultOpen
                    reason="The verification environment's Docker CLI does not provide the compose command."
                  >
                    {`docker: unknown command: docker compose
Run 'docker --help' for more information`}
                  </CommandOutput>
                ),
              },
            ]}
          />
        </VerificationRound>
        <HandoffAgent
          name="Code reviewer"
          state="running"
          status="running"
          meta={
            <>
              <span>example / preview-model</span>
              <span>Attempt 1</span>
              <span>Started 09:04</span>
            </>
          }
        >
          <p className="text-sm text-muted-foreground">
            Reviewing the pinned changes. Findings appear here when the review
            completes.
          </p>
        </HandoffAgent>
        <HandoffAgent
          name="Fixer"
          state="completed"
          status="completed"
          meta={
            <>
              <span>example / preview-model</span>
              <span>Attempt 1</span>
              <span>1m 12s</span>
            </>
          }
        >
          <HandoffResult>
            Rejected whitespace-only bearer tokens and added a regression test
            for the empty-token case.
          </HandoffResult>
        </HandoffAgent>
      </HandoffIteration>
      <HandoffIteration
        title="Iteration 1"
        count="2 handoffs"
        defaultOpen={false}
        summary={
          <>
            <span>2 completed</span>
            <span>implementer · reviewer</span>
          </>
        }
      >
        <HandoffAgent
          name="Code reviewer"
          state="completed"
          status="completed"
          meta={<span>example / preview-model</span>}
        >
          <HandoffResult verdict="needs-changes">
            One blocking finding must be addressed before approval.
          </HandoffResult>
          <Finding
            severity="blocking"
            location="src/middleware/auth.ts:10"
            note="resolved in iteration 2"
          >
            A header containing only "Bearer " passes the prefix check and
            yields an empty token that is sent to verifyToken.
          </Finding>
        </HandoffAgent>
      </HandoffIteration>
    </div>
  )
}

/* -------------------------------------------------------------------------- */
/* Usage                                                                      */
/* -------------------------------------------------------------------------- */

const usageRows = [
  {
    agent: "Implementer",
    status: "completed",
    model: "example / preview-model",
    input: "48,210",
    inputCost: "$0.1446",
    output: "6,905",
    outputCost: "$0.1036",
    total: "55,115",
    cost: "$0.2482",
  },
  {
    agent: "Code reviewer",
    status: "completed",
    model: "example / preview-model",
    input: "31,540",
    inputCost: "$0.0946",
    output: "2,120",
    outputCost: "$0.0318",
    total: "33,660",
    cost: "$0.1264",
  },
]

export function UsagePreview() {
  return (
    <div className="min-w-0 px-[30px] pt-7 pb-9 @max-[760px]/shell:px-5">
      <div className="mb-[26px] flex flex-wrap items-start justify-between gap-x-6 gap-y-3.5">
        <div>
          <h2 className="mb-1.5 text-lg font-semibold tracking-[-.02em]">
            Token usage & cost
          </h2>
          <p className="max-w-[75ch] text-sm/[1.65] text-muted-foreground">
            Recorded per agent execution and grouped by iteration. Cache writes
            are part of new input; thinking is part of output.
          </p>
        </div>
      </div>
      <MetricsSummary total="$0.4212" totalLabel="Run total">
        <span>
          <strong>115,600</strong> tokens
        </span>
        <span>
          <strong>4</strong> executions
        </span>
        <span>
          <strong>2</strong> iterations
        </span>
      </MetricsSummary>
      <MetricsTable label="Token usage by agent">
        <MetricsHeader>
          <MetricsRow variant="group">
            <th rowSpan={2}>Agent</th>
            <th rowSpan={2}>Model</th>
            <th colSpan={2} className="border-l">
              Input
            </th>
            <th colSpan={2} className="border-l">
              Output
            </th>
            <th colSpan={2} className="border-l">
              Totals
            </th>
          </MetricsRow>
          <MetricsRow variant="column">
            <th className="border-l">Tokens</th>
            <th>Cost</th>
            <th className="border-l">Tokens</th>
            <th>Cost</th>
            <th className="border-l">Tokens</th>
            <th>Cost</th>
          </MetricsRow>
        </MetricsHeader>
        <MetricsBody>
          <MetricsRow variant="band">
            <th colSpan={8}>
              Iteration 1
              <span className="ml-3 text-xs font-normal text-muted-foreground">
                2 executions
              </span>
            </th>
          </MetricsRow>
          {usageRows.map((row) => (
            <MetricsRow key={row.agent}>
              <td className="whitespace-normal">
                <span className="block font-[550]">{row.agent}</span>
                <span className="mt-1 flex items-center gap-[5px] text-xs text-muted-foreground capitalize">
                  <span className="size-[5px] rounded-full bg-signal-success" />
                  {row.status}
                </span>
              </td>
              <td className="whitespace-normal">{row.model}</td>
              <MetricValue
                value={row.input}
                secondary={row.inputCost}
                divider
              />
              <MetricValue value={row.inputCost} />
              <MetricValue
                value={row.output}
                secondary={row.outputCost}
                divider
              />
              <MetricValue value={row.outputCost} />
              <MetricValue value={row.total} divider />
              <MetricValue value={row.cost} strong />
            </MetricsRow>
          ))}
          <MetricsRow variant="subtotal">
            <th colSpan={2} className="text-left">
              Iteration 1 subtotal
            </th>
            <MetricValue value="79,750" divider />
            <MetricValue value="$0.2392" />
            <MetricValue value="9,025" divider />
            <MetricValue value="$0.1354" />
            <MetricValue value="88,775" divider />
            <MetricValue value="$0.3746" />
          </MetricsRow>
          <MetricsRow variant="band">
            <th colSpan={8}>
              Iteration 2
              <span className="ml-3 text-xs font-normal text-muted-foreground">
                2 executions · 1 running
              </span>
            </th>
          </MetricsRow>
          <MetricsRow>
            <td>
              <span className="block font-[550]">Code reviewer</span>
              <span className="mt-1 flex items-center gap-[5px] text-xs text-muted-foreground">
                <span className="size-[5px] rounded-full bg-signal-running" />
                Running
              </span>
            </td>
            <td>example / preview-model</td>
            <MetricValue value={<Unreported />} divider />
            <MetricValue value="—" />
            <MetricValue value={<Unreported />} divider />
            <MetricValue value="—" />
            <MetricValue value="—" divider />
            <MetricValue value="—" />
          </MetricsRow>
        </MetricsBody>
        <MetricsFooter>
          <MetricsRow variant="total">
            <th colSpan={2} className="text-left">
              Run total
            </th>
            <MetricValue value="104,890" divider />
            <MetricValue value="$0.3146" />
            <MetricValue value="10,710" divider />
            <MetricValue value="$0.1066" />
            <MetricValue value="115,600" divider />
            <MetricValue value="$0.4212" />
          </MetricsRow>
        </MetricsFooter>
      </MetricsTable>
      <div className="mt-[18px] grid gap-[5px] text-[0.8125rem] text-muted-foreground">
        <p>
          <strong className="font-medium text-foreground">New input</strong>{" "}
          includes cache writes. <Chip tone="neutral">Not reported</Chip> marks
          counts the provider has not returned yet.
        </p>
      </div>
    </div>
  )
}
