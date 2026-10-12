import * as React from "react"
import { useQuery, useQueryClient } from "@tanstack/react-query"

import { toast } from "@/components/ui/toast"
import { downloadBlob } from "@/features/builder/components/utils"
import type { AgentDocument } from "@/features/adk-workflows/lib/document"
import {
  downloadAdkRunFile,
  stepNameOf,
  type AdkRunFile,
  type AdkRunFileVersion,
  type AdkRunSessionFile,
} from "@/features/runs/lib/runs"
import { toApiError } from "@/lib/api/index"
import { formatBytes, formatDateTime } from "@/lib/format"

/*
 * A run's files (those it started with, those its agents saved) as its
 * pages list them and the file viewer opens them: fetched once for the
 * viewer, a download after reusing that copy.
 */

/** A run's file, as its lists and the viewer name it. */
export type RunFileRef = {
  name: string
  /** A saved file's version; none for a file the run started with (as it was sent). */
  version?: number
  mediaType?: string | null
  size?: number | null
  /** What the viewer says of it beside its type and size: who saved it, when. */
  detail?: string
}

/** One key per file and version, for lists and the viewer's cache. */
export const runFileKey = (file: RunFileRef) =>
  file.version === undefined ? file.name : `${file.name}@${file.version}`

export const contentKey = (
  organizationId: string,
  runId: string,
  file: RunFileRef
) =>
  [
    "adk-run-file",
    organizationId,
    runId,
    file.name,
    file.version ?? "sent",
  ] as const

/** A run's file, fetched once for the viewer and kept a while. */
export function useRunFileContent(
  organizationId: string,
  runId: string,
  file: RunFileRef | undefined
) {
  return useQuery({
    queryKey: file
      ? contentKey(organizationId, runId, file)
      : ["adk-run-file", organizationId, runId, "none"],
    queryFn: () =>
      downloadAdkRunFile(organizationId, runId, file!.name, file!.version),
    enabled: file !== undefined,
    staleTime: Infinity,
    gcTime: 5 * 60 * 1000,
    // The viewer shows why.
    meta: { silent: true },
  })
}

/**
 * Saves a run's file to the person's computer: the viewer's copy when it
 * has one. `fetching` names the file on its way.
 */
export function useRunFileDownload(organizationId: string, runId: string) {
  const client = useQueryClient()
  const [fetching, setFetching] = React.useState<string>()
  const download = React.useCallback(
    async (file: RunFileRef) => {
      setFetching(runFileKey(file))
      try {
        const kept = client.getQueryData<Blob>(
          contentKey(organizationId, runId, file)
        )
        const blob =
          kept ??
          (await downloadAdkRunFile(
            organizationId,
            runId,
            file.name,
            file.version
          ))
        downloadBlob(blob, file.name)
      } catch (error) {
        toast.add({
          title: `Couldn't download ${file.name}`,
          description: toApiError(error).message,
          type: "error",
        })
      } finally {
        setFetching(undefined)
      }
    },
    [client, organizationId, runId]
  )
  return { download, fetching }
}

/** What a saved file's version says of itself: its size, who saved it, when. */
export function savedVersionDetail(
  version: AdkRunFileVersion,
  doc: AgentDocument | undefined,
  { size = true }: { size?: boolean } = {}
) {
  return [
    size && version.size_bytes !== null
      ? formatBytes(version.size_bytes)
      : null,
    version.step
      ? `by ${stepNameOf({ id: version.step, name: "", kind: "" }, doc)}`
      : version.saved_by && `by ${version.saved_by}`,
    version.saved_at && formatDateTime(version.saved_at),
  ]
    .filter(Boolean)
    .join(" · ")
}

/** A saved file's version, as the viewer names it. */
export const savedFileRef = (
  file: AdkRunSessionFile,
  version: AdkRunFileVersion,
  doc: AgentDocument | undefined
): RunFileRef => ({
  name: file.name,
  version: version.version,
  mediaType: version.media_type ?? file.media_type,
  size: version.size_bytes,
  // Its size is the viewer's own.
  detail: savedVersionDetail(version, doc, { size: false }),
})

/** A file the run started with, as the viewer names it. */
export const inputFileRef = (file: AdkRunFile): RunFileRef => ({
  name: file.name,
  mediaType: file.media_type,
  size: file.size_bytes,
  detail: "the run started with it",
})
