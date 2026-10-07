import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"

import { createNestedResource } from "@/lib/api/resource"
import { api } from "@/lib/api-instance"

/*
 * An organization's code repositories, kept by the admin API: the GitHub
 * repositories it ingests into the code graph. Each ingestion is a job in
 * the code graph worker's queue (the admin API's MySQL); the worker claims
 * it, builds the graph and writes how it went back, which these read. The
 * graph is one per GitHub URL, shared by every organization that has the
 * repository, on the branch it was first ingested on. Reading takes
 * `organizations:read`; adding, removing and ingesting
 * `repositories:manage`.
 */

export type IngestionStatus =
  "QUEUED" | "RUNNING" | "SUCCEEDED" | "SUPERSEDED" | "FAILED"

/** What the graph an ingestion published holds, and what it left out. */
export type IngestionMetrics = {
  files?: number
  nodes?: number
  edges?: number
  resolved?: number
  unresolved?: number
  embedded?: number
  /** What it couldn't analyse, such as a module that didn't compile. */
  warning?: string
}

export type CodeIngestion = {
  id: string
  repository_id: string
  organization_id: string
  url: string
  branch: string
  /** The commit asked for, or the branch's head once the worker read it. */
  commit_sha: string | null
  requested_by: string
  status: IngestionStatus
  /** The tries it was charged. */
  attempts: number
  /** When a waiting one may run: later than now while it waits to retry. */
  eligible_at: string | null
  codegraph_repository_id: string | null
  run_id: string | null
  generation: number | null
  error_code: string
  error_message: string
  metrics: IngestionMetrics | null
  started_at: string | null
  finished_at: string | null
  created_at: string
  created_by: string
  updated_at: string
  updated_by: string
}

export type CodeRepository = {
  id: string
  organization_id: string
  /** `https://github.com/<owner>/<name>`, lowercase. */
  url: string
  /** The owner and name as they were written when added. */
  owner: string
  name: string
  branch: string
  latest_ingestion: CodeIngestion | null
  /** The latest ingestion that succeeded: its graph's counts. */
  last_success: CodeIngestion | null
  created_at: string
  created_by: string
  updated_at: string
  updated_by: string
}

export type CodeRepositoryCreate = {
  url: string
  branch: string
  /** Queue its first ingestion right away (the default). */
  ingest?: boolean
}

/** How often a page reads ingestions again while any is unfinished. */
export const INGESTION_POLL_MS = 3000

export const isActive = (status: IngestionStatus | undefined) =>
  status === "QUEUED" || status === "RUNNING"

export const organizationCodeRepositories = createNestedResource<
  CodeRepository,
  { organizationId: string },
  { create: CodeRepositoryCreate }
>({
  api,
  key: "organization-code-repositories",
  path: ({ organizationId }) =>
    `/organizations/${encodeURIComponent(organizationId)}/code-repositories`,
  label: "code repository",
})

/**
 * The organization's code repositories, each with its latest ingestion.
 * Reread every few seconds while any ingestion is queued or running.
 */
export function useOrganizationCodeRepositories(
  organizationId: string,
  { enabled = true }: { enabled?: boolean } = {}
) {
  return organizationCodeRepositories
    .scope({ organizationId })
    .useList(undefined, {
      enabled,
      refetchInterval: (query) =>
        query.state.data?.some((repository) =>
          isActive(repository.latest_ingestion?.status)
        )
          ? INGESTION_POLL_MS
          : false,
    })
}

const ingestionsPath = (organizationId: string, repositoryId: string) =>
  `/organizations/${encodeURIComponent(organizationId)}/code-repositories/${encodeURIComponent(repositoryId)}/ingestions`

/**
 * A repository's ingestions, newest first. Under the repositories' keys, so
 * their mutations refresh it; reread while any is unfinished. Waits while
 * `repositoryId` is undefined.
 */
export function useCodeIngestions(
  organizationId: string,
  repositoryId: string | undefined
) {
  const scoped = organizationCodeRepositories.scope({ organizationId })
  return useQuery({
    enabled: Boolean(repositoryId),
    queryKey: [...scoped.keys.all, "ingestions", repositoryId],
    queryFn: ({ signal }) =>
      api.get<CodeIngestion[]>(ingestionsPath(organizationId, repositoryId!), {
        params: { limit: 50 },
        signal,
      }),
    refetchInterval: (query) =>
      query.state.data?.some((job) => isActive(job.status))
        ? INGESTION_POLL_MS
        : false,
  })
}

/**
 * Queues an ingestion of a repository's branch (of `commit`, or its head);
 * answers the one already queued or running instead, when there is one.
 */
export function useIngestCodeRepository(organizationId: string) {
  const queryClient = useQueryClient()
  const scoped = organizationCodeRepositories.scope({ organizationId })
  return useMutation({
    mutationFn: ({
      repositoryId,
      commit,
    }: {
      repositoryId: string
      commit?: string
    }) =>
      api.post<CodeIngestion>(
        ingestionsPath(organizationId, repositoryId),
        commit ? { commit } : {}
      ),
    meta: { errorTitle: "Couldn't start the ingestion" },
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: scoped.keys.all }),
  })
}

/** Queues a failed ingestion again, its attempts reset. */
export function useRetryCodeIngestion(organizationId: string) {
  const queryClient = useQueryClient()
  const scoped = organizationCodeRepositories.scope({ organizationId })
  return useMutation({
    mutationFn: ({
      repositoryId,
      jobId,
    }: {
      repositoryId: string
      jobId: string
    }) =>
      api.post<CodeIngestion>(
        `${ingestionsPath(organizationId, repositoryId)}/${encodeURIComponent(jobId)}/retry`
      ),
    meta: { errorTitle: "Couldn't retry the ingestion" },
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: scoped.keys.all }),
  })
}
