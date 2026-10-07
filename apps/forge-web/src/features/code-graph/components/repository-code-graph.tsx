import type * as React from "react"

import { ViewToolbar } from "@/components/forge/app-shell"
import { PageEmpty } from "@/components/forge/empty-state"
import { Icon } from "@/components/forge/icon"
import { ShellToolbar } from "@/components/forge/shell"
import { Button } from "@/components/ui/button"
import { Spinner } from "@/components/ui/spinner"
import { toast } from "@/components/ui/toast"
import {
  isActive,
  useIngestCodeRepository,
  type CodeRepository,
} from "@/features/code-repositories/lib/api"
import { failureOf, fullName } from "@/features/code-repositories/lib/display"
import { CodeGraphExplorer } from "./code-graph-explorer"
import { IngestionFailure } from "./ingestion-failure"

/**
 * An organization's code repository as a code graph: the explorer once the
 * repository has ingested successfully (even while it re-ingests, which the
 * explorer notes), a waiting state while its first ingestion is queued or
 * runs, and otherwise what's wrong and a way to ingest it, for those who
 * may. The page that holds it rereads its repositories while one ingests,
 * so the explorer opens by itself when the ingestion succeeds.
 */
export function RepositoryCodeGraph({
  organizationId,
  repository,
  openAt,
  canIngest,
  tabs,
  active = true,
}: {
  organizationId: string
  repository: CodeRepository
  /** A node for the explorer to open at. */
  openAt?: string
  /** `repositories:manage` in the organization. */
  canIngest: boolean
  /** The repository's view tabs, first in the toolbar. */
  tabs?: React.ReactNode
  /** False while another of the repository's views has the page. */
  active?: boolean
}) {
  if (repository.last_success) {
    return (
      // One explorer per repository: another repository starts afresh.
      <CodeGraphExplorer
        key={repository.id}
        organizationId={organizationId}
        repository={repository}
        openAt={openAt}
        tabs={tabs}
        active={active}
      />
    )
  }
  const name = fullName(repository)
  const ingestion = repository.latest_ingestion
  const toolbar = active && tabs && (
    <ShellToolbar>
      <ViewToolbar>{tabs}</ViewToolbar>
    </ShellToolbar>
  )
  const github = (
    <Button
      variant="outline"
      size="sm"
      nativeButton={false}
      render={<a href={repository.url} target="_blank" rel="noreferrer" />}
    >
      <Icon icon="external" data-icon="inline-start" />
      Open on GitHub
    </Button>
  )
  if (ingestion && isActive(ingestion.status)) {
    const queued = ingestion.status === "QUEUED"
    const branch = ingestion.branch || repository.branch
    return (
      <>
        {toolbar}
        <PageEmpty
          illustration="waiting"
          title={
            queued
              ? `${repository.name} is queued`
              : `Ingesting ${repository.name}`
          }
          description={`The code graph worker ${queued ? "will read" : "is reading"} ${name}${branch ? ` at ${branch}` : ""}. Its graph opens here when it's done, usually within a few minutes.`}
        >
          <p
            role="status"
            className="flex items-center gap-1.5 text-xs text-muted-foreground"
          >
            <Spinner
              aria-hidden="true"
              role={undefined}
              className="size-3.5 text-signal-running"
            />
            {queued
              ? ingestion.error_code
                ? "Waiting to retry…"
                : "Waiting for a worker…"
              : "Ingesting…"}
          </p>
        </PageEmpty>
      </>
    )
  }
  const failed = ingestion?.status === "FAILED"
  // The worker's own words, shown below; and what its error code means,
  // when it's one Forge knows rather than that message again.
  const why = failed ? ingestion.error_message : ""
  const reason = failed ? failureOf(ingestion) : undefined
  const explained =
    reason && reason !== why && reason !== ingestion?.error_code
      ? reason
      : undefined
  return (
    <>
      {toolbar}
      <PageEmpty
        illustration={failed ? "error" : "search"}
        title={
          failed
            ? `${repository.name} didn't ingest`
            : `${repository.name} isn't in the code graph yet`
        }
        description={
          (failed
            ? `The last ingestion failed${ingestion.error_code ? ` (${ingestion.error_code})` : ""}. ${explained ? `${explained} ` : why ? "" : "The worker's logs say why. "}`
            : `Ingest it to explore ${name} as a graph of its files, types and members. `) +
          (canIngest
            ? "Forge reads it with the code graph worker's GitHub token."
            : "Ask someone who manages the organization's repositories to ingest it.")
        }
      >
        {why && (
          <IngestionFailure message={why} className="w-full max-w-[640px]" />
        )}
        <div className="flex flex-wrap justify-center gap-2">
          {canIngest && (
            <IngestButton
              organizationId={organizationId}
              repository={repository}
            />
          )}
          {github}
        </div>
      </PageEmpty>
    </>
  )
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
      size="sm"
      disabled={ingest.isPending}
      onClick={() =>
        ingest.mutate(
          { repositoryId: repository.id },
          {
            onSuccess: () =>
              toast.add({
                title: `Ingesting ${fullName(repository)}`,
                description:
                  "Its graph opens here once it's in the code graph.",
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
      {repository.latest_ingestion ? "Ingest again" : "Ingest"}
    </Button>
  )
}
