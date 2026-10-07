import * as React from "react"

import { Stat, StatGrid } from "@/components/forge/activity"
import { ViewToolbar } from "@/components/forge/app-shell"
import {
  createColumnHelper,
  DataTable,
  type DataTableFeatureConfig,
} from "@/components/forge/data-table"
import { PanelEmpty } from "@/components/forge/empty-state"
import { ErrorCallout } from "@/components/forge/feedback"
import { Icon } from "@/components/forge/icon"
import { ShellToolbar } from "@/components/forge/shell"
import { Chip } from "@/components/forge/status"
import {
  PageSection,
  RecordList,
  RecordRow,
} from "@/components/forge/task-page"
import { ToolbarButton } from "@/components/forge/toolbar"
import type { ChipTone } from "@/components/forge/variants"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import { Spinner } from "@/components/ui/spinner"
import { toast } from "@/components/ui/toast"
import { ADMIN_TABLE_FEATURES } from "@/features/admin/components/table-config"
import {
  useIngestCodeRepository,
  type CodeRepository,
} from "@/features/code-repositories/lib/api"
import {
  failureOf,
  fullName,
  statusDisplay,
} from "@/features/code-repositories/lib/display"
import { parseTimestamp } from "@/lib/timestamps"
import { useActorLabel } from "@/lib/users"
import { graphErrorMessage } from "../lib/api"
import {
  isWorking,
  useRepositoryStats,
  type CodeGraphStats,
  type GraphRun,
} from "../lib/stats"
import { IngestionFailure } from "./ingestion-failure"

/**
 * A repository's Ingestion tab: what the system holds of it, to audit its
 * code graph ingestions. It shows the live graph (nodes, edges, what search
 * can find and how much of it is embedded), what the latest run found and
 * wrote, and the latest runs. It refreshes every few seconds while the
 * repository is being ingested, and every minute otherwise.
 */
export function IngestionOverview({
  organizationId,
  repository,
  canIngest,
  tabs,
}: {
  organizationId: string
  repository: CodeRepository
  /** `repositories:manage` in the organization. */
  canIngest: boolean
  /** The repository's view tabs, first in the toolbar. */
  tabs: React.ReactNode
}) {
  const stats = useRepositoryStats(organizationId, repository)
  const working = isWorking(stats.data, repository)
  const toolbar = (
    <ShellToolbar>
      <ViewToolbar>
        {tabs}
        <div className="ml-auto flex min-w-0 items-center gap-[7px]">
          {stats.data && (
            <p className="flex min-w-0 items-center gap-1.5 truncate text-xs text-muted-foreground @max-[600px]/shell:hidden">
              {working && (
                <Spinner
                  aria-hidden="true"
                  role={undefined}
                  className="size-3 shrink-0 text-signal-running"
                />
              )}
              {working ? "Updating every few seconds" : "Updated"}{" "}
              {time(stats.data.as_of)}
            </p>
          )}
          <ToolbarButton
            icon="refresh"
            disabled={stats.isFetching}
            onClick={() => void stats.refetch()}
          >
            Refresh
          </ToolbarButton>
        </div>
      </ViewToolbar>
    </ShellToolbar>
  )

  if (stats.isPending) {
    return (
      <>
        {toolbar}
        <div className={PAGE} aria-busy="true">
          <Skeleton className="h-5 w-40" />
          <Skeleton className="h-52" />
          <Skeleton className="h-52" />
        </div>
      </>
    )
  }
  if (stats.error) {
    return (
      <>
        {toolbar}
        <ErrorCallout
          title="Couldn't read the ingestion stats"
          action={
            <Button
              variant="outline"
              size="sm"
              onClick={() => void stats.refetch()}
            >
              Retry
            </Button>
          }
        >
          {graphErrorMessage(stats.error)}
        </ErrorCallout>
      </>
    )
  }
  return (
    <>
      {toolbar}
      <div className={PAGE}>
        <CodeGraphSection
          organizationId={organizationId}
          repository={repository}
          canIngest={canIngest}
          stats={stats.data.code_graph}
        />
      </div>
    </>
  )
}

const PAGE = "flex max-w-[1180px] flex-col gap-4 pb-10"
// Two columns of facts on a wide shell, one on a narrow one.
const COLUMNS =
  "grid grid-cols-2 gap-x-10 gap-y-8 @max-[1050px]/shell:grid-cols-1"

/* Code graph -------------------------------------------------------------- */

function CodeGraphSection({
  organizationId,
  repository,
  canIngest,
  stats,
}: {
  organizationId: string
  repository: CodeRepository
  canIngest: boolean
  stats: CodeGraphStats
}) {
  const { totals, runs } = stats
  const latest = runs[0]
  const embeddings = totals?.embeddings
  const ingestion = repository.latest_ingestion
  // A failed ingestion the worker's runs don't show: it failed before the
  // worker ran it, or its run is no longer among the latest.
  const unlisted =
    ingestion?.status === "FAILED" &&
    !runs.some((run) => run.run_id === ingestion.run_id)
  return (
    <PageSection
      title="Code graph"
      meta={
        <span className="flex items-center gap-2">
          {ingestion ? (
            <IngestionStatus ingestion={ingestion} />
          ) : (
            latest && <RunPhase phase={latest.phase} />
          )}
          {canIngest && (
            <IngestButton
              organizationId={organizationId}
              repository={repository}
            />
          )}
        </span>
      }
      description={`What the code graph worker parsed from ${fullName(repository)} and published: the live graph's files, declarations and relationships, which of them search can find, and how many of those are embedded.`}
    >
      {unlisted && (
        <ErrorCallout title="The latest ingestion failed" className="mb-4">
          {failureOf(ingestion) ||
            ingestion.error_message ||
            "The worker's logs say why."}
        </ErrorCallout>
      )}
      {stats.status === "unavailable" ? (
        <ErrorCallout title="Couldn't read the code graph">
          {stats.error || "The code graph worker didn't answer."}
        </ErrorCallout>
      ) : stats.status === "not_ingested" || !totals ? (
        <Panel>
          {latest?.phase === "FAILED" ? (
            <>
              <PanelEmpty illustration="error">
                {`The last ingestion failed, so ${repository.name} isn't in the code graph yet.`}
              </PanelEmpty>
              {latest.error_message && (
                <IngestionFailure
                  message={latest.error_message}
                  className="mx-5 mb-5"
                />
              )}
            </>
          ) : (
            <PanelEmpty illustration="search">
              {stats.error ||
                `${repository.name} isn't in the code graph yet. Its numbers show here once it's ingested.`}
            </PanelEmpty>
          )}
        </Panel>
      ) : (
        <div className="flex flex-col gap-8">
          <StatGrid className="grid-cols-6 @max-[1270px]/shell:grid-cols-3">
            <Stat label="Nodes" value={count(totals.nodes)} />
            <Stat label="Edges" value={count(totals.edges)} />
            <Stat label="Files" value={count(totals.node_kinds.source_file)} />
            <Stat label="Searchable" value={count(totals.searchable)} />
            <Stat
              label="Embedded"
              value={
                <Share part={embeddings?.current} whole={totals.searchable} />
              }
            />
            <Stat
              label="Generation"
              value={totals.generation > 0 ? totals.generation : "—"}
            />
          </StatGrid>
          <div className={COLUMNS}>
            <div className="flex flex-col gap-8">
              {latest && <LatestRun run={latest} />}
              <Facts title="Search index">
                <RecordRow label="Live commit">
                  <Commit branch={totals.branch} sha={totals.commit_sha} />
                </RecordRow>
                <RecordRow label="Model">
                  {embeddings?.model ? (
                    `${embeddings.model} · ${embeddings.dimensions} dimensions`
                  ) : (
                    <Muted>Not embedding</Muted>
                  )}
                </RecordRow>
                <RecordRow label="Up to date">
                  {count(embeddings?.current)} of {count(totals.searchable)}{" "}
                  searchable nodes
                </RecordRow>
                <RecordRow label="Stored vectors">
                  {count(embeddings?.stored)}
                  {embeddings && embeddings.stored > embeddings.current && (
                    <Muted>
                      {" "}
                      · {count(embeddings.stored - embeddings.current)} for
                      nodes changed or retired since
                    </Muted>
                  )}
                </RecordRow>
              </Facts>
            </div>
            <div className="flex flex-col gap-8">
              {latest?.metrics?.durations && (
                <Facts title="Latest run's stages">
                  <Breakdown
                    label="Stage durations"
                    items={Object.entries(latest.metrics.durations)}
                    format={duration}
                    sort={false}
                  />
                </Facts>
              )}
              <Facts title="Nodes by kind">
                <Breakdown
                  label="Nodes by kind"
                  items={Object.entries(totals.node_kinds)}
                />
              </Facts>
              <Facts title="Edges by kind">
                <Breakdown
                  label="Edges by kind"
                  items={Object.entries(totals.edge_kinds)}
                />
              </Facts>
            </div>
          </div>
          {runs.length > 0 && <RunsTable runs={runs} />}
        </div>
      )}
    </PageSection>
  )
}

/** What the latest run found and wrote. */
function LatestRun({ run }: { run: GraphRun }) {
  const actor = useActorLabel()
  const m = run.metrics ?? {}
  const index = run.index
  return (
    <Facts title="Latest run">
      <RecordRow label="Status">
        <span className="flex flex-wrap items-center gap-2">
          <RunPhase phase={run.phase} />
          {run.error_code && <Muted>{run.error_code}</Muted>}
        </span>
      </RecordRow>
      {run.error_message && (
        <RecordRow label="Why it failed">
          <IngestionFailure message={run.error_message} />
        </RecordRow>
      )}
      {run.warning_message && (
        <RecordRow label="Left unresolved">
          <IngestionFailure
            message={run.warning_message}
            label="What the ingestion left unresolved"
          />
        </RecordRow>
      )}
      <RecordRow label="Commit">
        <Commit branch={run.branch} sha={run.commit_sha} />
      </RecordRow>
      {run.requested_by && (
        <RecordRow label="Requested by">{actor(run.requested_by)}</RecordRow>
      )}
      <RecordRow label="Ran">
        {run.started_at ? time(run.started_at) : <Muted>Not started</Muted>}
        {run.started_at && run.finished_at && (
          <Muted> · took {between(run.started_at, run.finished_at)}</Muted>
        )}
        {run.attempts > 1 && <Muted> · {run.attempts} attempts</Muted>}
      </RecordRow>
      <RecordRow label="Files">
        {count(m.files)} discovered · {count(m.affected_files)} parsed
      </RecordRow>
      <RecordRow label="Symbols">
        {count(m.symbols)} declared · {count(m.resolved)} references resolved
        {(m.unresolved ?? 0) + (m.ambiguous ?? 0) > 0 && (
          <Muted>
            {" "}
            · {count(m.unresolved)} unresolved, {count(m.ambiguous)} ambiguous
          </Muted>
        )}
      </RecordRow>
      <RecordRow label="Written">
        {count(m.added)} added · {count(m.updated)} updated · {count(m.retired)}{" "}
        retired
        {(m.reopened ?? 0) > 0 && ` · ${count(m.reopened)} reopened`}
      </RecordRow>
      <RecordRow label="Embeddings">
        {index ? (
          <>
            {count(index.embedded)} computed
            {index.requests ? ` in ${count(index.requests)} requests` : ""}
            <Muted>
              {" "}
              · {INDEX_STATUS[index.status ?? "RUNNING"]}
              {index.error ? `: ${index.error}` : ""}
            </Muted>
          </>
        ) : (
          <Muted>None</Muted>
        )}
      </RecordRow>
    </Facts>
  )
}

const INDEX_STATUS = {
  RUNNING: "embedding",
  COMPLETE: "complete",
  INCOMPLETE: "incomplete",
} as const

// A few runs: sort, size, hide and export them.
const FEATURES: Partial<DataTableFeatureConfig> = {
  ...ADMIN_TABLE_FEATURES,
  globalFilter: false,
  pagination: false,
  faceting: false,
}

/** A row of the runs table: one run of the code graph worker. */
type RunRow = {
  id: string
  /** Undefined until it's accepted. */
  accepted: string | undefined
  phase: string
  commit: string
  /** In ms; undefined until it finishes. */
  took: number | undefined
  files: number
  added: number
  updated: number
  retired: number
  embedded: number
  /** Who it fetched as; empty when nobody. */
  by: string
}

const runColumn = createColumnHelper<RunRow>()

/** A right-aligned count of what a run did. */
const countColumn = (
  id: "files" | "added" | "updated" | "retired" | "embedded",
  label: string,
  size: number
) =>
  runColumn.accessor(id, {
    header: label,
    size,
    meta: { label, align: "right" },
    cell: ({ getValue }) => count(getValue()),
  })

const RUN_COLUMNS = runColumn.columns([
  runColumn.accessor("accepted", {
    header: "Accepted",
    // The column that stretches; this is its narrowest.
    size: 132,
    sortUndefined: "last",
    meta: { label: "Accepted" },
    cell: ({ getValue }) => {
      const at = getValue()
      return at ? time(at) : "—"
    },
  }),
  runColumn.accessor("phase", {
    header: "Status",
    size: 116,
    meta: { label: "Status" },
    cell: ({ getValue }) => <RunPhase phase={getValue()} />,
  }),
  runColumn.accessor("commit", {
    header: "Commit",
    size: 96,
    meta: { label: "Commit" },
    cell: ({ getValue }) => (
      <code className="font-mono text-2xs" title={getValue()}>
        {getValue().slice(0, 7) || "—"}
      </code>
    ),
  }),
  runColumn.accessor("took", {
    header: "Took",
    size: 88,
    sortUndefined: "last",
    meta: { label: "Took", align: "right" },
    cell: ({ getValue }) => {
      const took = getValue()
      return took === undefined ? "—" : duration(took)
    },
  }),
  countColumn("files", "Files parsed", 116),
  countColumn("added", "Added", 88),
  countColumn("updated", "Updated", 96),
  countColumn("retired", "Retired", 92),
  countColumn("embedded", "Embedded", 108),
  runColumn.accessor("by", {
    header: "By",
    size: 168,
    meta: { label: "By", cellClassName: "text-muted-foreground" },
    cell: ({ getValue }) => getValue() || "—",
  }),
])

function RunsTable({ runs }: { runs: GraphRun[] }) {
  const actor = useActorLabel()
  // Who asked by email rather than user ID, to sort and export.
  const rows = React.useMemo(
    () =>
      runs.map((run): RunRow => ({
        id: run.run_id,
        accepted: run.accepted_at ?? undefined,
        phase: run.phase,
        commit: run.commit_sha,
        took:
          run.started_at && run.finished_at
            ? parseTimestamp(run.finished_at).getTime() -
              parseTimestamp(run.started_at).getTime()
            : undefined,
        files: run.metrics?.affected_files ?? 0,
        added: run.metrics?.added ?? 0,
        updated: run.metrics?.updated ?? 0,
        retired: run.metrics?.retired ?? 0,
        embedded: run.index?.embedded ?? 0,
        by: run.requested_by ? actor(run.requested_by) : "",
      })),
    [runs, actor]
  )
  return (
    <Facts title="Latest runs" plain>
      <DataTable
        columns={RUN_COLUMNS}
        data={rows}
        getRowId={(row) => row.id}
        features={FEATURES}
        stateKey="ingestion-graph-runs"
        exportFileName="code-graph-runs"
      />
    </Facts>
  )
}

// The code graph worker's run phases, which differ from the admin API's
// ingestion statuses: a run is accepted, not queued.
const RUN_PHASE: Record<string, { label: string; tone: ChipTone }> = {
  ACCEPTED: { label: "Queued", tone: "notice" },
  RUNNING: { label: "Ingesting", tone: "notice" },
  SUCCEEDED: { label: "Succeeded", tone: "success" },
  FAILED: { label: "Failed", tone: "danger" },
  SUPERSEDED: { label: "Superseded", tone: "neutral" },
}

function RunPhase({ phase }: { phase: string }) {
  const { label, tone } = RUN_PHASE[phase] ?? { label: phase, tone: "neutral" }
  return <Chip tone={tone}>{label}</Chip>
}

/** Where the repository's latest ingestion stands, as its list says it. */
function IngestionStatus({
  ingestion,
}: {
  ingestion: NonNullable<CodeRepository["latest_ingestion"]>
}) {
  const { label, tone } = statusDisplay(ingestion)
  return <Chip tone={tone}>{label}</Chip>
}

function IngestButton({
  organizationId,
  repository,
}: {
  organizationId: string
  repository: CodeRepository
}) {
  const ingest = useIngestCodeRepository(organizationId)
  return (
    <Button
      variant="outline"
      size="xs"
      disabled={ingest.isPending}
      onClick={() =>
        ingest.mutate(
          { repositoryId: repository.id },
          {
            onSuccess: () =>
              toast.add({
                title: `Ingesting ${fullName(repository)}`,
                description: "Its numbers here follow the run.",
                type: "success",
              }),
          }
        )
      }
    >
      {ingest.isPending ? (
        <Spinner data-icon="inline-start" />
      ) : (
        <Icon icon="refresh" data-icon="inline-start" />
      )}
      {repository.latest_ingestion ? "Re-ingest" : "Ingest"}
    </Button>
  )
}

/* Parts ------------------------------------------------------------------- */

/** A titled group of facts; `plain` for a table instead of record rows. */
function Facts({
  title,
  plain = false,
  children,
}: {
  title: string
  plain?: boolean
  children: React.ReactNode
}) {
  return (
    <section className="min-w-0">
      <h3 className="mb-3 text-sm font-[550]">{title}</h3>
      {plain ? children : <RecordList>{children}</RecordList>}
    </section>
  )
}

function Panel({ children }: { children: React.ReactNode }) {
  return (
    <div className="rounded-(--radius-card) border bg-card">{children}</div>
  )
}

function Muted({ children }: { children: React.ReactNode }) {
  return <span className="text-muted-foreground">{children}</span>
}

function Commit({ branch, sha }: { branch: string; sha: string }) {
  return (
    <span className="flex min-w-0 items-center gap-1.5">
      <Icon icon="branch" size={13} className="shrink-0" />
      <span className="truncate">{branch || "—"}</span>
      {sha && (
        <code className="font-mono text-2xs text-muted-foreground" title={sha}>
          {sha.slice(0, 12)}
        </code>
      )}
    </span>
  )
}

/** A count and its share of a whole, e.g. "1,263 · 100%". */
function Share({ part, whole }: { part: number | undefined; whole: number }) {
  return (
    <>
      {count(part)}
      {whole > 0 && (
        <span className="ml-2 text-sm text-muted-foreground">
          {percent(part ?? 0, whole)}
        </span>
      )}
    </>
  )
}

/** Labelled counts as bars, largest first unless told otherwise. */
function Breakdown({
  label,
  items,
  format = count,
  sort = true,
  limit = 12,
}: {
  label: string
  items: [string, number][]
  format?: (value: number) => string
  sort?: boolean
  limit?: number
}) {
  const rows = sort ? [...items].sort((a, b) => b[1] - a[1]) : items
  const shown = rows.slice(0, limit)
  const rest = rows.slice(limit).reduce((sum, [, n]) => sum + n, 0)
  const max = Math.max(1, ...shown.map(([, n]) => n))
  if (rows.length === 0) return <Muted>None</Muted>
  return (
    <ul aria-label={label} className="flex flex-col gap-2 border-t pt-3">
      {shown.map(([name, value]) => (
        <li
          key={name}
          className="grid grid-cols-[9rem_minmax(0,1fr)_5rem] items-center gap-3 text-xs"
        >
          <span className="truncate text-muted-foreground" title={name}>
            {name.replaceAll("_", " ")}
          </span>
          <span aria-hidden="true" className="h-1.5 rounded-full bg-muted">
            <span
              className="block h-full rounded-full bg-chart-2"
              style={{ width: `${Math.max(2, (value / max) * 100)}%` }}
            />
          </span>
          <span className="text-right tabular-nums">{format(value)}</span>
        </li>
      ))}
      {rest > 0 && (
        <li className="text-2xs text-muted-foreground">
          and {format(rest)} more in {rows.length - limit} others
        </li>
      )}
    </ul>
  )
}

/* Formatting -------------------------------------------------------------- */

const numbers = new Intl.NumberFormat()
const count = (value: number | undefined) => numbers.format(value ?? 0)

const percent = (part: number, whole: number) => {
  const share = (part / whole) * 100
  // 99.6% of a big graph isn't all of it.
  return `${share >= 99.95 && part < whole ? "99.9" : share < 10 ? share.toFixed(1) : Math.round(share)}%`
}

const timeFormat = new Intl.DateTimeFormat(undefined, {
  month: "short",
  day: "numeric",
  hour: "numeric",
  minute: "2-digit",
})
const time = (at: string) => timeFormat.format(parseTimestamp(at))

function duration(ms: number) {
  if (ms < 1000) return `${Math.round(ms)} ms`
  const seconds = Math.round(ms / 1000)
  if (seconds < 60) return `${seconds}s`
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60)
    return `${minutes}m ${String(seconds % 60).padStart(2, "0")}s`
  return `${Math.floor(minutes / 60)}h ${String(minutes % 60).padStart(2, "0")}m`
}

const between = (from: string, to: string) =>
  duration(parseTimestamp(to).getTime() - parseTimestamp(from).getTime())
