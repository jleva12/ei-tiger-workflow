import { Tabs } from "@/components/ui/tabs"
import { PageEmpty } from "@/components/forge/empty-state"
import { ErrorCallout } from "@/components/forge/feedback"
import { Button } from "@/components/ui/button"
import {
  TaskPage,
  TaskTabBar,
  TaskTabsList,
  TaskTabsTrigger,
} from "@/components/forge/task-page"
import { tasks } from "../data"
import {
  ChangesPreview,
  HandoffPreview,
  RunLogPreview,
  RunSelector,
  SubmissionPreview,
  TaskDetailPreview,
  TaskHeaderPreview,
  UsagePreview,
} from "../detail-previews"
import { SectionPage, Specimen } from "../specimen"

const task = tasks.find((item) => item.id === "FG-214")!

export function TaskPageSection() {
  return (
    <SectionPage
      icon="view"
      eyebrow="Detail pages"
      title="Task header & tabs"
      description="Detail pages share the 14px --text-ui size. The header pairs a 26px title with status, repository and branch meta and outline actions; underlined tabs sit on a hairline with run controls on the right."
    >
      <Specimen
        title="Header & tab bar"
        variant="flush"
        code={`<TaskPage>
  <TaskPageHeader>
    <TaskPageEyebrow onBack={back} reference="FG-214" label="Read-only example" />
    <TaskTitleRow
      title="Test worker reconnection"
      meta={<>
        <StatusBadge status="review" size="lg" />
        <TaskMeta icon="folder">workspace/coding-agent</TaskMeta>
        <TaskMeta icon="branch">main</TaskMeta>
      </>}
      actions={<Button variant="outline" size="sm">Copy link</Button>}
    />
  </TaskPageHeader>
  <Tabs defaultValue="submission" className="gap-0">
    <TaskTabBar>
      <TaskTabsList>
        <TaskTabsTrigger value="logs" icon="activity" live>Run logs</TaskTabsTrigger>
      </TaskTabsList>
      <LiveLabel online>Live</LiveLabel>
    </TaskTabBar>
  </Tabs>
</TaskPage>`}
      >
        <TaskPage>
          <TaskHeaderPreview task={task} />
          <Tabs defaultValue="logs" className="gap-0">
            <TaskTabBar className="border-b-0">
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
          </Tabs>
        </TaskPage>
      </Specimen>
      <Specimen
        title="Complete detail page"
        description="Every tab is live — switch between them."
        variant="flush"
      >
        <TaskDetailPreview task={task} />
      </Specimen>
      <Specimen title="Errors & empty tab">
        <div className="flex flex-col gap-4">
          <ErrorCallout
            action={
              <Button variant="outline" size="sm">
                Retry
              </Button>
            }
          >
            Could not load run #3. The admin API returned 502 Bad Gateway.
          </ErrorCallout>
          <PageEmpty
            illustration="waiting"
            title="No run has been recorded yet"
            description="The task is registered. Its submission will appear here when a run is created."
          />
        </div>
      </Specimen>
    </SectionPage>
  )
}

export function SubmissionSection() {
  return (
    <SectionPage
      icon="message"
      eyebrow="Detail pages"
      title="Submission"
      description="Pending and queued runs show their immutable submission and execution plan: a readable brief, a numbered agent timeline with disclosures, a two-column record and a hairline aside of configuration facts. A warm notice explains waiting states."
    >
      <Specimen
        title="Submission tab"
        variant="flush"
        code={`<SubmissionLayout aside={<AsideSection title="Run configuration">
  <AsideFact label="Base branch">main</AsideFact>
</AsideSection>}>
  <WaitingNotice title="Waiting for a worker" state="Queued">…</WaitingNotice>
  <PageSection title="Execution plan" meta="3 configured agents" description="…">
    <ExecutionPlan>
      <ExecutionStep step="01" completed name="Implementer" role="implementer" status="completed">
        <Disclosure summary="Agent instructions"><pre>…</pre></Disclosure>
      </ExecutionStep>
    </ExecutionPlan>
  </PageSection>
  <PageSection title="Submission record">
    <RecordList><RecordRow label="Task ID"><code>FG-214</code></RecordRow></RecordList>
  </PageSection>
</SubmissionLayout>`}
      >
        <TaskPage>
          <SubmissionPreview waiting />
        </TaskPage>
      </Specimen>
    </SectionPage>
  )
}

export function RunLogsSection() {
  return (
    <SectionPage
      icon="code"
      eyebrow="Detail pages"
      title="Run logs"
      description="A GitHub Actions–inspired job list beside an always-dark, searchable console. Entries are native disclosures with line numbers, kind glyphs, coloured states and timestamps; the scroll region is keyboard-focusable."
    >
      <Specimen
        title="Jobs + console"
        variant="flush"
        code={`<RunLogLayout>
  <JobsSidebar>
    <JobsLabel trailing="#3">Workflow</JobsLabel>
    <JobButton icon="activity" name="All events" count={5} active />
    <JobsLabel>Agent jobs</JobsLabel>
    <JobButton state="running" name="Code reviewer" details={["Iteration 2 · Attempt 1"]} />
    <RunStats><RunStat label="Duration">3m 42s</RunStat></RunStats>
  </JobsSidebar>
  <LogPane>
    <LogPaneHeading title="Workflow activity" state="running" subtitle="Code reviewer is working" />
    <LogConsole>
      <LogToolbar>
        <LogSearch />
        <LogToolbarText>5 events</LogToolbarText>
        <LogToolbarButton aria-pressed={follow}>Following</LogToolbarButton>
      </LogToolbar>
      <LogScroll>
        <LogEntry number={1} kind="tool" status="completed" title="edit" time="09:03:11" />
      </LogScroll>
      <LogFooter online>Connected to task event stream</LogFooter>
    </LogConsole>
  </LogPane>
</RunLogLayout>`}
      >
        <TaskPage>
          <RunLogPreview />
        </TaskPage>
      </Specimen>
    </SectionPage>
  )
}

export function ChangesSection() {
  return (
    <SectionPage
      icon="review"
      eyebrow="Detail pages"
      title="Changes & diffs"
      description="An immutable, line-numbered diff beside a searchable changed-file navigator. Additions and deletions use GitHub's tints, adapted for dark mode; totals use the diff-added / diff-removed tokens."
    >
      <Specimen
        title="Changes tab"
        variant="flush"
        code={`<DiffWorkspace>
  <FileNavigator>
    <FileFilter />
    <FileListLabel count={3} />
    <FileList>
      <ChangedFile path="src/middleware/auth.ts" added={12} removed={2} active />
    </FileList>
  </FileNavigator>
  <FileDiff>
    <FileDiffHeader path="src/middleware/auth.ts" added={12} removed={2}
      actions={<Button variant="ghost" size="sm">Wrap</Button>} />
    <DiffView lines={lines} />
  </FileDiff>
</DiffWorkspace>`}
      >
        <TaskPage>
          <ChangesPreview className="h-[620px]" />
        </TaskPage>
      </Specimen>
    </SectionPage>
  )
}

export function HandoffSection() {
  return (
    <SectionPage
      icon="commit"
      eyebrow="Detail pages"
      title="Handoff & verification"
      description="Iterations group saved prompts, evidence, agent results and review findings, latest first. Each opens with the harness verification: commands, exit codes and output. “Unable to verify” is always amber — never styled as a pass or an observed failure."
    >
      <Specimen
        title="Handoff tab"
        variant="flush"
        code={`<HandoffIteration title="Iteration 2" latest count="2 handoffs"
  summary={<><span>1 completed · 1 running</span><span>fixer · reviewer</span></>}>
  <VerificationRound title="Harness verification" result="passed" meta="via .forge.md steps">
    <VerificationCommands commands={[
      { result: "passed", command: "npm test", source: "forge", exitCode: 0, duration: "1.2 s" },
      { result: "unable_to_verify", command: "docker compose config", details: <CommandOutput … /> },
    ]} />
  </VerificationRound>
  <HandoffAgent name="Code reviewer" state="completed" status="completed">
    <HandoffResult verdict="needs-changes">One blocking finding…</HandoffResult>
    <Finding severity="blocking" location="src/middleware/auth.ts:10">…</Finding>
  </HandoffAgent>
</HandoffIteration>`}
      >
        <TaskPage>
          <HandoffPreview />
        </TaskPage>
      </Specimen>
    </SectionPage>
  )
}

export function MetricsSection() {
  return (
    <SectionPage
      icon="dashboard"
      eyebrow="Detail pages"
      title="Metrics table"
      description="Dense numeric tables with combined headers, iteration bands, subtotals and a totals footer. Values use tabular numerals with secondary lines (e.g. cost under tokens); the frame scrolls horizontally and is keyboard-focusable."
    >
      <Specimen
        title="Token usage / cost"
        variant="flush"
        code={`<MetricsTable label="Token usage by agent">
  <MetricsHeader>
    <MetricsRow variant="group">…</MetricsRow>
    <MetricsRow variant="column">…</MetricsRow>
  </MetricsHeader>
  <MetricsBody>
    <MetricsRow variant="band"><th colSpan={8}>Iteration 1</th></MetricsRow>
    <MetricsRow>
      <td>Implementer</td>
      <MetricValue value="48,210" secondary="$0.1446" divider />
    </MetricsRow>
    <MetricsRow variant="subtotal">…</MetricsRow>
  </MetricsBody>
  <MetricsFooter><MetricsRow variant="total">…</MetricsRow></MetricsFooter>
</MetricsTable>`}
      >
        <TaskPage>
          <UsagePreview />
        </TaskPage>
      </Specimen>
    </SectionPage>
  )
}
