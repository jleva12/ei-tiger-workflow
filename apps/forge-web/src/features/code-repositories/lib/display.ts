import { SourceCodeIcon } from "@hugeicons/core-free-icons"

import type { IconProp } from "@/components/forge/icons"
import type { CodeIngestion, IngestionMetrics } from "./api"

export const CODE_REPOSITORIES_ICON: IconProp = SourceCodeIcon

type ChipTone = "neutral" | "success" | "warning" | "danger" | "notice"

/**
 * How an ingestion stands, as a chip says it. A queued one that failed
 * before waits to be retried.
 */
export function statusDisplay(
  job: Pick<CodeIngestion, "status" | "error_code">
): { label: string; tone: ChipTone } {
  switch (job.status) {
    case "QUEUED":
      return job.error_code
        ? { label: "Retrying", tone: "warning" }
        : { label: "Queued", tone: "neutral" }
    case "RUNNING":
      return { label: "Ingesting", tone: "notice" }
    case "SUCCEEDED":
      return { label: "Ingested", tone: "success" }
    case "SUPERSEDED":
      return { label: "Superseded", tone: "neutral" }
    case "FAILED":
      return { label: "Failed", tone: "danger" }
  }
}

/** Why ingestions fail, in words, by the code the worker records. */
const FAILURES: Record<string, string> = {
  repository_unreadable:
    "Forge can't read the repository. Check its URL; for a private repository, the GitHub token the code graph worker uses must read it.",
  branch_not_found: "The repository has no such branch.",
  branch_conflict:
    "The repository's code graph follows another branch; add it on that branch.",
  invalid_repository: "That isn't a GitHub repository's URL.",
  retry_exhausted: "It failed on every attempt.",
  job_timeout: "It ran past the worker's time limit.",
  transient_failure: "A service it needs didn't answer; it will be retried.",
}

/** Why an ingestion failed (or last failed, while it waits to retry). */
export function failureOf(
  job: Pick<CodeIngestion, "error_code" | "error_message">
): string | undefined {
  if (!job.error_code) return undefined
  return FAILURES[job.error_code] ?? (job.error_message || job.error_code)
}

const count = (n: number) => n.toLocaleString()

/** A graph's size, e.g. "545 nodes · 2,086 edges". */
export function graphSize(metrics: IngestionMetrics | null | undefined) {
  if (metrics?.nodes === undefined) return undefined
  return `${count(metrics.nodes)} nodes · ${count(metrics.edges ?? 0)} edges`
}

export const shortSha = (sha: string | null | undefined) =>
  sha ? sha.slice(0, 7) : undefined

/** `owner/name` as written when added. */
export const fullName = (repository: { owner: string; name: string }) =>
  `${repository.owner}/${repository.name}`
