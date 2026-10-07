import { useQuery } from "@tanstack/react-query"

import {
  isActive,
  organizationCodeRepositories,
  type CodeRepository,
} from "@/features/code-repositories/lib/api"
import { api } from "@/lib/api-instance"
import { repositoryPath } from "./api"

/**
 * What the system holds of a code repository, to audit its ingestions: its
 * code graph as the code graph worker reports it, passed through by the
 * admin API. A worker that can't tell says why in `code_graph`, and the
 * stats still answer.
 */

/** The live (published) graph; generation 0 until one is published. */
export type GraphTotals = {
  branch: string
  generation: number
  commit_sha: string
  run_id: string
  nodes: number
  edges: number
  node_kinds: Record<string, number>
  edge_kinds: Record<string, number>
  /** Nodes search can find: declarations and code chunks. */
  searchable: number
  embeddings: {
    /** What the worker embeds with; empty when it doesn't. */
    model: string
    dimensions: number
    /** Searchable nodes whose stored vector matches what they are now. */
    current: number
    /** Vectors stored for the repository with that model, current or not. */
    stored: number
  }
}

/** What a run found and wrote; every count is optional in older runs. */
export type RunMetrics = {
  files?: number
  affected_files?: number
  contexts?: number
  symbols?: number
  lookups?: number
  resolved?: number
  unresolved?: number
  ambiguous?: number
  unsupported?: number
  nodes?: number
  edges?: number
  added?: number
  updated?: number
  retired?: number
  reopened?: number
  embedded?: number
  invalidated_contexts?: number
  /** Milliseconds per stage: prepare, fetch, build, discover, parse… */
  durations?: Record<string, number>
}

/** A run's embedding pass. */
export type RunIndex = {
  model?: string
  dimensions?: number
  status?: "RUNNING" | "COMPLETE" | "INCOMPLETE"
  embedded?: number
  requests?: number
  started_at?: string
  finished_at?: string
  error?: string
}

/** One run of the code graph worker. */
export type GraphRun = {
  run_id: string
  phase: string
  branch: string
  commit_sha: string
  /** The Forge user it fetched as. */
  requested_by: string
  /** The generation it published; 0 until it does. */
  generation: number
  accepted_at: string | null
  started_at: string | null
  finished_at: string | null
  attempts: number
  failed_attempts: number
  error_code: string
  /** Why it failed, in the worker's words; empty otherwise. */
  error_message: string
  /**
   * What it could not analyse although it went on, one warning per line;
   * empty otherwise.
   */
  warning_message: string
  metrics: RunMetrics | null
  index: RunIndex | null
}

export type CodeGraphStats = {
  /**
   * `not_ingested` when it has never been ingested or the worker no longer
   * has it; `unavailable` when the worker isn't set up or didn't answer.
   */
  status: "ok" | "not_ingested" | "unavailable"
  error: string
  totals: GraphTotals | null
  /** Its latest runs, newest first. */
  runs: GraphRun[]
}

export type RepositoryStats = {
  repository_id: string
  code_graph: CodeGraphStats
  as_of: string
}

/** How often the stats refresh while something is working on the repository. */
export const STATS_ACTIVE_POLL_MS = 5000
// …and otherwise, while they're on screen.
const STATS_IDLE_POLL_MS = 60_000

/**
 * Whether the repository is being ingested, so the stats are about to
 * change: the worker's latest run is queued or running, or the repository's
 * latest ingestion is still waiting for the worker or on it.
 */
export function isWorking(
  stats: RepositoryStats | undefined,
  repository: Pick<CodeRepository, "latest_ingestion">
) {
  const run = stats?.code_graph.runs[0]
  return (
    run?.phase === "ACCEPTED" ||
    run?.phase === "RUNNING" ||
    isActive(repository.latest_ingestion?.status)
  )
}

/**
 * A code repository's stats, refreshed every few seconds while it's being
 * ingested. Keyed under the organization's code repositories, so ingesting
 * it refreshes them.
 */
export function useRepositoryStats(
  organizationId: string,
  repository: CodeRepository
) {
  const scoped = organizationCodeRepositories.scope({ organizationId })
  return useQuery({
    queryKey: [...scoped.keys.all, "stats", repository.id],
    queryFn: ({ signal }) =>
      api.get<RepositoryStats>(
        `${repositoryPath(organizationId, repository.id)}/stats`,
        { params: { runs: 10 }, signal }
      ),
    refetchInterval: (query) =>
      isWorking(query.state.data, repository)
        ? STATS_ACTIVE_POLL_MS
        : STATS_IDLE_POLL_MS,
  })
}
