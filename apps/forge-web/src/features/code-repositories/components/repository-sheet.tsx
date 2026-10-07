import { Copyable } from "@/components/forge/copyable"
import { ErrorCallout } from "@/components/forge/feedback"
import { Icon } from "@/components/forge/icon"
import { Chip } from "@/components/forge/status"
import {
  DetailList,
  DetailRow,
  TaskSheetContent,
  TaskSheetHeader,
} from "@/components/forge/task-sheet"
import { Button } from "@/components/ui/button"
import { Sheet } from "@/components/ui/sheet"
import { Skeleton } from "@/components/ui/skeleton"
import { Spinner } from "@/components/ui/spinner"
import { useLastNode } from "@/features/admin/components/dialog-state"
import { formatDuration, formatRelative } from "@/lib/format"
import {
  isActive,
  useCodeIngestions,
  useIngestCodeRepository,
  useRetryCodeIngestion,
  type CodeIngestion,
  type CodeRepository,
} from "../lib/api"
import {
  CODE_REPOSITORIES_ICON,
  failureOf,
  fullName,
  graphSize,
  shortSha,
  statusDisplay,
} from "../lib/display"

/**
 * A code repository: its branch, the graph its latest successful ingestion
 * published, and every ingestion, newest first, with what failed. Those who
 * manage it ingest it again, retry a failed ingestion, or remove it.
 */
export function RepositorySheet({
  organizationId,
  repository,
  open,
  onOpenChange,
  canManage,
  onRemove,
}: {
  organizationId: string
  repository: CodeRepository | undefined
  open: boolean
  onOpenChange: (open: boolean) => void
  canManage: boolean
  onRemove: (repository: CodeRepository) => void
}) {
  // While it closes, it keeps showing what it showed.
  const shown = useLastNode(repository)
  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <TaskSheetContent>
        {shown && (
          <RepositoryDetails
            organizationId={organizationId}
            repository={open ? (repository ?? shown) : shown}
            canManage={canManage}
            onRemove={onRemove}
          />
        )}
      </TaskSheetContent>
    </Sheet>
  )
}

function RepositoryDetails({
  organizationId,
  repository,
  canManage,
  onRemove,
}: {
  organizationId: string
  repository: CodeRepository
  canManage: boolean
  onRemove: (repository: CodeRepository) => void
}) {
  const ingestions = useCodeIngestions(organizationId, repository.id)
  const ingest = useIngestCodeRepository(organizationId)
  const retry = useRetryCodeIngestion(organizationId)
  const latest = repository.latest_ingestion
  const success = repository.last_success
  const busy = isActive(latest?.status)
  const jobs = ingestions.data ?? []

  return (
    <>
      <TaskSheetHeader
        eyebrow="Code repository"
        eyebrowIcon={CODE_REPOSITORIES_ICON}
        title={fullName(repository)}
        description={`Ingested from the ${repository.branch} branch. Its code graph is shared with every organization that has the repository.`}
      />
      <div className="grid gap-6 overflow-y-auto px-5 pb-6" tabIndex={0}>
        {canManage && (
          <div className="flex flex-wrap gap-2">
            <Button
              size="sm"
              disabled={busy || ingest.isPending}
              onClick={() => ingest.mutate({ repositoryId: repository.id })}
            >
              {ingest.isPending ? (
                <Spinner data-icon="inline-start" />
              ) : (
                <Icon icon="refresh" data-icon="inline-start" />
              )}
              {busy ? "Ingestion in progress" : "Ingest now"}
            </Button>
            <Button
              size="sm"
              variant="destructive"
              onClick={() => onRemove(repository)}
            >
              Remove…
            </Button>
          </div>
        )}

        <DetailList>
          <DetailRow label="Repository">
            <a
              className="text-foreground underline-offset-2 hover:underline"
              href={repository.url}
              target="_blank"
              rel="noreferrer"
            >
              {repository.url}
            </a>
          </DetailRow>
          <DetailRow label="Branch">
            <span className="font-mono text-2xs">{repository.branch}</span>
          </DetailRow>
          <DetailRow label="Status">
            {latest ? (
              <Chip tone={statusDisplay(latest).tone}>
                {statusDisplay(latest).label}
              </Chip>
            ) : (
              <span className="text-muted-foreground">Not ingested yet</span>
            )}
          </DetailRow>
          <DetailRow label="Code graph">
            {success ? (
              <span className="flex flex-col gap-0.5">
                <span>{graphSize(success.metrics) ?? "Published"}</span>
                <span className="text-2xs text-muted-foreground">
                  {[
                    success.metrics?.files !== undefined &&
                      `${success.metrics.files.toLocaleString()} files`,
                    success.generation && `generation ${success.generation}`,
                    shortSha(success.commit_sha) &&
                      `commit ${shortSha(success.commit_sha)}`,
                    success.finished_at && formatRelative(success.finished_at),
                  ]
                    .filter(Boolean)
                    .join(" · ")}
                </span>
              </span>
            ) : (
              <span className="text-muted-foreground">
                None yet: it's published by its first successful ingestion.
              </span>
            )}
          </DetailRow>
        </DetailList>

        {success?.metrics?.warning && (
          <ErrorCallout title="Ingested with gaps">
            {success.metrics.warning}
          </ErrorCallout>
        )}

        {success?.codegraph_repository_id && (
          <Copyable
            label="Code graph repository ID (for the code graph's tools)"
            value={success.codegraph_repository_id}
          />
        )}

        <section className="grid gap-2">
          <h3 className="text-xs font-medium text-muted-foreground">
            Ingestions
          </h3>
          {ingestions.error ? (
            <ErrorCallout
              title="Couldn't load the ingestions"
              action={
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => void ingestions.refetch()}
                >
                  Retry
                </Button>
              }
            >
              {ingestions.error.message}
            </ErrorCallout>
          ) : ingestions.isPending ? (
            <div className="grid gap-2">
              <Skeleton className="h-14" />
              <Skeleton className="h-14" />
            </div>
          ) : jobs.length === 0 ? (
            <p className="text-muted-foreground">
              No ingestions yet.
              {canManage && " Ingest it to publish its code graph."}
            </p>
          ) : (
            <ul className="grid gap-2">
              {jobs.map((job) => (
                <IngestionItem
                  key={job.id}
                  job={job}
                  onRetry={
                    canManage && job.status === "FAILED" && !busy
                      ? () =>
                          retry.mutate({
                            repositoryId: repository.id,
                            jobId: job.id,
                          })
                      : undefined
                  }
                  retrying={
                    retry.isPending && retry.variables?.jobId === job.id
                  }
                />
              ))}
            </ul>
          )}
        </section>
      </div>
    </>
  )
}

function IngestionItem({
  job,
  onRetry,
  retrying,
}: {
  job: CodeIngestion
  onRetry?: () => void
  retrying: boolean
}) {
  const display = statusDisplay(job)
  const failure = failureOf(job)
  const took =
    job.started_at && job.finished_at
      ? Date.parse(job.finished_at) - Date.parse(job.started_at)
      : undefined
  return (
    <li className="grid gap-1.5 rounded-(--radius-item) border border-border bg-card px-3 py-2.5">
      <div className="flex items-center gap-2">
        <Chip tone={display.tone}>{display.label}</Chip>
        <span className="font-mono text-2xs text-muted-foreground">
          {shortSha(job.commit_sha) ?? `${job.branch} head`}
        </span>
        <span className="ml-auto text-2xs text-muted-foreground">
          {formatRelative(job.created_at)}
        </span>
      </div>
      <div className="text-2xs text-muted-foreground">
        {[
          graphSize(job.metrics),
          took !== undefined && `took ${formatDuration(took)}`,
          job.attempts > 1 && `${job.attempts} attempts`,
        ]
          .filter(Boolean)
          .join(" · ") || "Waiting for the code graph worker"}
      </div>
      {failure && (
        <p
          className={
            job.status === "FAILED"
              ? "text-2xs text-destructive"
              : "text-2xs text-muted-foreground"
          }
        >
          {failure}
        </p>
      )}
      {onRetry && (
        <div>
          <Button
            size="xs"
            variant="outline"
            disabled={retrying}
            onClick={onRetry}
          >
            {retrying && <Spinner data-icon="inline-start" />}
            Retry
          </Button>
        </div>
      )}
    </li>
  )
}
