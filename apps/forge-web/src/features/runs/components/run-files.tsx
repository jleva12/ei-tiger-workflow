import * as React from "react"
import { Download04Icon } from "@hugeicons/core-free-icons"
import { cn } from "cn"

import {
  canPreview,
  FilePreview,
  PreviewButton,
  PreviewDivider,
} from "@/components/forge/file-preview"
import { Icon } from "@/components/forge/icon"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogTitle,
} from "@/components/ui/dialog"
import { Spinner } from "@/components/ui/spinner"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { KINDS, kindOf } from "@/features/knowledge/lib/knowledge"
import {
  runFileKey,
  useRunFileContent,
  useRunFileDownload,
  type RunFileRef,
} from "@/features/runs/lib/run-files"
import { toApiError } from "@/lib/api/index"
import { formatBytes } from "@/lib/format"

/*
 * A run's files (those it started with, those its agents saved), each
 * previewed in the file viewer the knowledge bases' documents open in, or
 * downloaded. The viewer fetches a file once; a download after reuses it.
 */

/** Preview and Download for one of a run's files. */
export function RunFileActions({
  file,
  fetching,
  onPreview,
  onDownload,
}: {
  file: RunFileRef
  fetching: boolean
  onPreview: () => void
  onDownload: () => void
}) {
  const version =
    file.version === undefined ? "" : `, version ${file.version + 1}`
  return (
    <span className="flex shrink-0 items-center">
      <Tooltip>
        <TooltipTrigger
          render={
            <Button
              variant="ghost"
              size="icon-xs"
              aria-label={`Preview ${file.name}${version}`}
              onClick={onPreview}
            />
          }
        >
          <Icon icon="view" />
        </TooltipTrigger>
        <TooltipContent>
          {canPreview(file.name, file.mediaType ?? undefined)
            ? "Preview"
            : "Preview (this kind of file may only download)"}
        </TooltipContent>
      </Tooltip>
      <Tooltip>
        <TooltipTrigger
          render={
            <Button
              variant="ghost"
              size="icon-xs"
              aria-label={`Download ${file.name}${version}`}
              disabled={fetching}
              onClick={onDownload}
            />
          }
        >
          {fetching ? <Spinner /> : <Icon icon="download" />}
        </TooltipTrigger>
        <TooltipContent>Download</TooltipContent>
      </Tooltip>
    </span>
  )
}

/** A file's mark, by its kind, as the knowledge bases show theirs. */
export function RunFileIcon({
  name,
  size = 15,
  className,
}: {
  name: string
  size?: number
  className?: string
}) {
  return (
    <Icon
      icon={KINDS[kindOf(name)].icon}
      size={size}
      className={cn("shrink-0 text-muted-foreground", className)}
    />
  )
}

/** A file's name, opening it in the viewer. */
export function RunFileName({
  file,
  onPreview,
  className,
}: {
  file: RunFileRef
  onPreview: () => void
  className?: string
}) {
  return (
    <button
      type="button"
      onClick={onPreview}
      title={`Preview ${file.name}`}
      className={cn(
        "min-w-0 flex-1 truncate text-left text-sm text-foreground underline-offset-2 hover:underline",
        className
      )}
    >
      {file.name}
    </button>
  )
}

/**
 * A run's file, opened over the page in the file viewer: PDFs, Office
 * documents, sheets, Markdown, text and images shown as they are; ← and →
 * page through the files listed with it; Download saves it; Esc closes.
 */
export function RunFileViewer({
  organizationId,
  runId,
  files,
  open,
  onOpen,
  onClose,
}: {
  organizationId: string
  runId: string
  /** The files listed with it, to page through in order. */
  files: RunFileRef[]
  /** The file shown; none closes the viewer. */
  open: RunFileRef | undefined
  onOpen: (file: RunFileRef) => void
  onClose: () => void
}) {
  const content = useRunFileContent(organizationId, runId, open)
  const { download, fetching } = useRunFileDownload(organizationId, runId)
  const key = open ? runFileKey(open) : undefined
  const index = files.findIndex((file) => runFileKey(file) === key)
  const previous = index > 0 ? files[index - 1] : undefined
  const next = index >= 0 ? files[index + 1] : undefined

  const onKeyDown = (event: React.KeyboardEvent) => {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return
    if (event.altKey || event.metaKey || event.ctrlKey || event.shiftKey) return
    const target = event.target as HTMLElement
    if (
      target.closest(
        "input, textarea, [role=grid], [role=tablist], [role=menu]"
      )
    )
      return
    const to = event.key === "ArrowLeft" ? previous : next
    if (!to) return
    event.preventDefault()
    onOpen(to)
  }

  const size = open?.size ?? content.data?.size
  const mediaType = open?.mediaType || content.data?.type || undefined
  return (
    <Dialog
      open={open !== undefined}
      onOpenChange={(next) => !next && onClose()}
    >
      <DialogContent
        showCloseButton={false}
        onKeyDown={onKeyDown}
        className="flex h-[calc(100dvh-2rem)] flex-col gap-0 overflow-hidden p-0 max-[600px]:h-dvh max-[600px]:max-w-full max-[600px]:rounded-none sm:max-w-[min(96rem,calc(100%-2rem))]"
      >
        {open && (
          <>
            <header className="flex h-14 shrink-0 items-center gap-3 border-b pr-2.5 pl-4 max-[600px]:pl-3">
              <span className="flex size-8 shrink-0 items-center justify-center rounded-(--radius-soft) bg-muted">
                <RunFileIcon name={open.name} size={16} />
              </span>
              <div className="grid min-w-0 flex-1 gap-0.5">
                <DialogTitle
                  className="truncate text-sm leading-5 font-medium"
                  title={open.name}
                >
                  {open.name}
                </DialogTitle>
                <DialogDescription className="truncate text-xs text-muted-foreground">
                  {[
                    mediaType,
                    size !== undefined && size !== null
                      ? formatBytes(size)
                      : null,
                    open.version !== undefined
                      ? `version ${open.version + 1}`
                      : null,
                    open.detail,
                  ]
                    .filter(Boolean)
                    .join(" · ") || " "}
                </DialogDescription>
              </div>
              <div className="flex shrink-0 items-center gap-0.5">
                {index >= 0 && files.length > 1 && (
                  <>
                    <PreviewButton
                      label="Previous file"
                      icon="left"
                      shortcut="←"
                      disabled={!previous}
                      onClick={() => previous && onOpen(previous)}
                    />
                    <span className="px-1 text-xs text-muted-foreground tabular-nums max-[600px]:hidden">
                      {index + 1} of {files.length}
                    </span>
                    <PreviewButton
                      label="Next file"
                      icon="right"
                      shortcut="→"
                      disabled={!next}
                      onClick={() => next && onOpen(next)}
                    />
                    <PreviewDivider />
                  </>
                )}
                <PreviewButton
                  label="Download"
                  icon={Download04Icon}
                  disabled={fetching === key}
                  onClick={() => void download(open)}
                />
                <PreviewDivider />
                <DialogClose
                  render={
                    <Button
                      variant="ghost"
                      size="icon-sm"
                      aria-label="Close"
                      className="text-muted-foreground hover:text-foreground"
                    />
                  }
                >
                  <Icon icon="close" size={16} />
                </DialogClose>
              </div>
            </header>
            <div className="relative flex min-h-0 flex-1">
              <FilePreview
                key={key}
                name={open.name}
                mediaType={mediaType}
                size={size ?? undefined}
                data={content.data}
                error={content.error ? toApiError(content.error).message : null}
                onRetry={() => void content.refetch()}
                onDownload={() => void download(open)}
              />
            </div>
          </>
        )}
      </DialogContent>
    </Dialog>
  )
}
