import * as React from "react"
import { cn } from "cn"
import {
  Copy01Icon,
  Delete02Icon,
  Download04Icon,
  Folder01Icon,
  FolderExportIcon,
  InformationCircleIcon,
  Link01Icon,
} from "@hugeicons/core-free-icons"

import { Illustration } from "@/components/forge/illustration"
import {
  FilePreview,
  PreviewButton,
  PreviewDivider,
} from "@/components/forge/file-preview"
import { Icon } from "@/components/forge/icon"
import { StatusBadge } from "@/components/forge/status"
import { DetailList, DetailRow } from "@/components/forge/task-sheet"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogTitle,
} from "@/components/ui/dialog"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { Skeleton } from "@/components/ui/skeleton"
import { toast } from "@/components/ui/toast"
import {
  INDEXING_POLL_MS,
  knowledgeDocuments,
  useDocumentContent,
  useDownloadDocument,
  type DocumentCollection,
  type KnowledgeDocument,
  type KnowledgeScope,
} from "../lib/api"
import {
  canRetry,
  collectionChoices,
  describeFailure,
  extensionOf,
  formatBytes,
  formatCount,
  formatDate,
  formatWhen,
  INDEX_STATES,
  isUnfinished,
  KINDS,
  kindOf,
  stateOf,
  type CollectionTree,
} from "../lib/knowledge"

export type DocumentViewerProps = {
  scope: KnowledgeScope
  /** The document to show. */
  documentId: string
  /** The documents listed behind the viewer, to page through in order. */
  documents: KnowledgeDocument[]
  tree: CollectionTree
  collections: DocumentCollection[] | undefined
  /** May retry, move and remove documents (`knowledge_bases:manage`). */
  canManage: boolean
  nameOf: (user: string) => string
  me: string | undefined
  /** Shows another document in the viewer. */
  onOpen: (documentId: string) => void
  onClose: () => void
  onMove: (document: KnowledgeDocument, collectionId: string | null) => void
  onRetry: (document: KnowledgeDocument) => void
  onRemove: (document: KnowledgeDocument) => void
}

/**
 * A knowledge base's document, opened over its page: the file itself in the
 * file viewer, what it is and where it stands in the search index beside
 * it, and the way to the previous and next document of the list behind.
 * Anyone in the organization downloads it; those who manage knowledge bases
 * also retry its indexing, move or remove it from here. ← and → page through the list; Esc closes.
 */
export function DocumentViewer({
  scope,
  documentId,
  documents,
  tree,
  collections,
  canManage,
  nameOf,
  me,
  onOpen,
  onClose,
  onMove,
  onRetry,
  onRemove,
}: DocumentViewerProps) {
  const listed = documents.find((each) => each.id === documentId)
  const detail = knowledgeDocuments.scope(scope).useDetail(documentId, {
    placeholderData: listed,
    // Its index state as the worker reports it, while that's changing.
    refetchInterval: (query) =>
      query.state.data && isUnfinished(query.state.data.phase)
        ? INDEXING_POLL_MS
        : false,
  })
  const document = detail.data
  const content = useDocumentContent(scope, document)
  const download = useDownloadDocument(scope)
  const [details, setDetails] = useDetailsOpen()

  const index = documents.findIndex((each) => each.id === documentId)
  const previous = index > 0 ? documents[index - 1] : undefined
  const next = index >= 0 ? documents[index + 1] : undefined

  // ← and → page through the list, unless something inside wants them.
  const onKeyDown = (event: React.KeyboardEvent) => {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return
    if (event.altKey || event.metaKey || event.ctrlKey || event.shiftKey) return
    const target = event.target as HTMLElement
    if (target.closest(KEEPS_ARROWS)) return
    const scroller = target.closest<HTMLElement>("[role=document], [role=img]")
    if (scroller && scroller.scrollWidth > scroller.clientWidth) return
    const to = event.key === "ArrowLeft" ? previous : next
    if (!to) return
    event.preventDefault()
    onOpen(to.id)
  }

  const copyLink = () =>
    void navigator.clipboard.writeText(window.location.href).then(
      () => toast.add({ type: "success", title: "Copied the link" }),
      () => toast.add({ type: "error", title: "Couldn't copy the link" })
    )

  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent
        showCloseButton={false}
        onKeyDown={onKeyDown}
        className="flex h-[calc(100dvh-2rem)] flex-col gap-0 overflow-hidden p-0 max-[600px]:h-dvh max-[600px]:max-w-full max-[600px]:rounded-none sm:max-w-[min(112rem,calc(100%-2rem))]"
      >
        <header className="flex h-14 shrink-0 items-center gap-3 border-b pr-2.5 pl-4 max-[600px]:pl-3">
          <span className="flex size-8 shrink-0 items-center justify-center rounded-(--radius-soft) bg-muted text-muted-foreground">
            <Icon
              icon={KINDS[kindOf(document?.filename ?? "")].icon}
              size={16}
            />
          </span>
          <div className="grid min-w-0 flex-1 gap-0.5">
            <DialogTitle
              className="truncate text-sm leading-5 font-medium"
              title={document?.filename}
            >
              {document?.filename ??
                (detail.isPending ? "Opening…" : "Document")}
            </DialogTitle>
            <DialogDescription className="truncate text-xs text-muted-foreground">
              {document
                ? [
                    kindName(document.filename),
                    formatBytes(document.size_bytes),
                    document.collection_id
                      ? tree.pathName(document.collection_id) || "Unfiled"
                      : "Unfiled",
                  ].join(" · ")
                : " "}
            </DialogDescription>
          </div>
          {document && (
            <StatusBadge
              status={INDEX_STATES[stateOf(document.phase)].status}
              className="shrink-0 max-[760px]:hidden"
            >
              {INDEX_STATES[stateOf(document.phase)].label}
            </StatusBadge>
          )}
          <div className="flex shrink-0 items-center gap-0.5">
            {index >= 0 && documents.length > 1 && (
              <>
                <PreviewButton
                  label="Previous document"
                  icon="left"
                  shortcut="←"
                  disabled={!previous}
                  onClick={() => previous && onOpen(previous.id)}
                />
                <span className="px-1 text-xs text-muted-foreground tabular-nums max-[600px]:hidden">
                  {formatCount(index + 1)} of {formatCount(documents.length)}
                </span>
                <PreviewButton
                  label="Next document"
                  icon="right"
                  shortcut="→"
                  disabled={!next}
                  onClick={() => next && onOpen(next.id)}
                />
                <PreviewDivider />
              </>
            )}
            <PreviewButton
              label={details ? "Hide details" : "Show details"}
              icon={InformationCircleIcon}
              pressed={details}
              onClick={() => setDetails(!details)}
            />
            <PreviewButton
              label="Download"
              icon={Download04Icon}
              disabled={!document || download.isPending}
              onClick={() => document && download.mutate(document)}
            />
            {document && (
              <ActionsMenu
                document={document}
                canManage={canManage}
                collections={collections}
                tree={tree}
                onCopyLink={copyLink}
                onMove={onMove}
                onRetry={onRetry}
                onRemove={onRemove}
              />
            )}
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
          {document ? (
            <FilePreview
              name={document.filename}
              mediaType={document.media_type}
              size={document.size_bytes}
              data={content.data}
              progress={content.progress}
              error={content.error?.message}
              onRetry={() => void content.refetch()}
              onDownload={() => download.mutate(document)}
            />
          ) : detail.error ? (
            <Gone status={detail.error.status} message={detail.error.message} />
          ) : (
            <div className="flex flex-1 items-center justify-center bg-muted">
              <Skeleton className="h-[70%] w-[min(40rem,80%)] rounded-(--radius-card)" />
            </div>
          )}
          {details && document && (
            <Details
              document={document}
              tree={tree}
              canManage={canManage}
              nameOf={nameOf}
              me={me}
              onRetry={onRetry}
            />
          )}
        </div>
      </DialogContent>
    </Dialog>
  )
}

// What keeps ← and → for itself: fields, menus, tabs, the sheet grid.
const KEEPS_ARROWS = [
  "input",
  "textarea",
  "select",
  "[contenteditable=true]",
  "[role=menu]",
  "[role=menuitem]",
  "[role=listbox]",
  "[role=tablist]",
  "[role=group]",
  "[role=radiogroup]",
  "[role=grid]",
  "[data-slot=table-container]",
].join(", ")

const DETAILS_KEY = "forge.knowledge.viewer.details"

/**
 * Whether the details panel shows: as last left, else when there's room for
 * it beside the file.
 */
function useDetailsOpen() {
  const [open, setOpen] = React.useState(() => {
    try {
      const stored = window.localStorage.getItem(DETAILS_KEY)
      if (stored !== null) return stored === "1"
    } catch {
      // Storage off: fall back to the room there is.
    }
    return window.innerWidth >= 1100
  })
  const change = React.useCallback((next: boolean) => {
    setOpen(next)
    try {
      window.localStorage.setItem(DETAILS_KEY, next ? "1" : "0")
    } catch {
      // Remembered for this visit only.
    }
  }, [])
  return [open, change] as const
}

/** "PDF", "Word document", "Spreadsheet": what kind of file it is. */
function kindName(filename: string) {
  const kind = kindOf(filename)
  const extension = extensionOf(filename)
  switch (kind) {
    case "pdf":
      return "PDF"
    case "word":
      return "Word document"
    case "slides":
      return "PowerPoint deck"
    case "sheets":
      return extension === "csv" || extension === "tsv"
        ? extension.toUpperCase()
        : "Spreadsheet"
    case "text":
      return ["md", "markdown", "mdx"].includes(extension) ? "Markdown" : "Text"
    case "diagrams":
      return extension.startsWith("vsd") ? "Visio drawing" : "Mermaid diagram"
    default:
      return extension ? `.${extension} file` : "File"
  }
}

/** Where a deep link to a document that's gone lands. */
function Gone({ status, message }: { status?: number; message: string }) {
  return (
    <div className="flex flex-1 flex-col items-center justify-center gap-3 bg-muted p-8 text-center">
      <Illustration name={status === 404 ? "search" : "error"} />
      <p className="text-[0.9375rem] font-medium text-foreground">
        {status === 404
          ? "This document isn't here anymore"
          : "Couldn't open this document"}
      </p>
      <p className="max-w-[26rem] text-xs/relaxed text-muted-foreground">
        {status === 404
          ? "It was removed from the knowledge base, or the link is wrong."
          : message}
      </p>
      <DialogClose render={<Button variant="outline" size="sm" />}>
        Back to the documents
      </DialogClose>
    </div>
  )
}

/** Copy its link; for a manager, retry, move and remove. */
function ActionsMenu({
  document,
  canManage,
  collections,
  tree,
  onCopyLink,
  onMove,
  onRetry,
  onRemove,
}: {
  document: KnowledgeDocument
  canManage: boolean
  collections: DocumentCollection[] | undefined
  tree: CollectionTree
  onCopyLink: () => void
  onMove: (document: KnowledgeDocument, collectionId: string | null) => void
  onRetry: (document: KnowledgeDocument) => void
  onRemove: (document: KnowledgeDocument) => void
}) {
  return (
    <DropdownMenu>
      <DropdownMenuTrigger
        render={
          <Button
            variant="ghost"
            size="icon-sm"
            aria-label={`More actions for ${document.filename}`}
            className="text-muted-foreground hover:text-foreground"
          />
        }
      >
        <Icon icon="more" size={16} />
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="min-w-52">
        <DropdownMenuItem onClick={onCopyLink}>
          <Icon icon={Link01Icon} />
          Copy link
        </DropdownMenuItem>
        {canManage && (
          <>
            <DropdownMenuSeparator />
            {canRetry(document.phase) && (
              <DropdownMenuItem onClick={() => onRetry(document)}>
                <Icon icon="refresh" />
                Retry indexing
              </DropdownMenuItem>
            )}
            <DropdownMenuSub>
              <DropdownMenuSubTrigger>
                <Icon icon={FolderExportIcon} />
                Move to collection
              </DropdownMenuSubTrigger>
              <DropdownMenuSubContent className="min-w-44">
                <DropdownMenuRadioGroup
                  value={document.collection_id ?? ""}
                  onValueChange={(value) => {
                    const next = String(value) || null
                    if (next !== document.collection_id) onMove(document, next)
                  }}
                >
                  <DropdownMenuRadioItem value="" closeOnClick>
                    Unfiled
                  </DropdownMenuRadioItem>
                  {collectionChoices(collections, tree).map(
                    ({ collection, path }) => (
                      <DropdownMenuRadioItem
                        key={collection.id}
                        value={collection.id}
                        closeOnClick
                      >
                        <span className="truncate" title={path}>
                          {path}
                        </span>
                      </DropdownMenuRadioItem>
                    )
                  )}
                </DropdownMenuRadioGroup>
              </DropdownMenuSubContent>
            </DropdownMenuSub>
            <DropdownMenuSeparator />
            <DropdownMenuItem
              variant="destructive"
              onClick={() => onRemove(document)}
            >
              <Icon icon={Delete02Icon} />
              Remove…
            </DropdownMenuItem>
          </>
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

/**
 * Beside the file: what it is, who added it where, and whether the
 * agents can search it (and why not). On a narrow screen it lays over
 * the file.
 */
function Details({
  document,
  tree,
  canManage,
  nameOf,
  me,
  onRetry,
}: {
  document: KnowledgeDocument
  tree: CollectionTree
  canManage: boolean
  nameOf: (user: string) => string
  me: string | undefined
  onRetry: (document: KnowledgeDocument) => void
}) {
  const state = stateOf(document.phase)
  const who = (user: string) => (user === me ? "You" : nameOf(user))
  const copySha = () =>
    void navigator.clipboard.writeText(document.sha256).then(
      () => toast.add({ type: "success", title: "Copied the SHA-256" }),
      () => toast.add({ type: "error", title: "Couldn't copy the SHA-256" })
    )
  return (
    <aside
      aria-label="Document details"
      className="w-[19rem] shrink-0 overflow-y-auto border-l bg-background max-[900px]:absolute max-[900px]:inset-y-0 max-[900px]:right-0 max-[900px]:z-10 max-[900px]:shadow-(--shadow-float) max-[600px]:w-full"
    >
      <section className="border-b px-5 py-5">
        <h3 className="mb-4 text-xs font-medium text-foreground">Details</h3>
        <DetailList className="mx-0 gap-3.5 border-0 py-0 max-[600px]:mx-0">
          <DetailRow label="Kind">{kindName(document.filename)}</DetailRow>
          <DetailRow label="Size">
            <span className="tabular-nums">
              {formatBytes(document.size_bytes)}
            </span>
          </DetailRow>
          <DetailRow label="Collection">
            {document.collection_id ? (
              <span className="flex min-w-0 items-center gap-1.5">
                <Icon
                  icon={Folder01Icon}
                  size={13}
                  className="shrink-0 text-subtle"
                />
                <span
                  className="truncate"
                  title={tree.pathName(document.collection_id)}
                >
                  {tree.pathName(document.collection_id) || "Unfiled"}
                </span>
              </span>
            ) : (
              <span className="text-subtle">Unfiled</span>
            )}
          </DetailRow>
          <DetailRow label="Uploaded by">
            <span className="truncate">{who(document.created_by)}</span>
          </DetailRow>
          <DetailRow label="Uploaded">
            <span title={formatWhen(document.created_at)}>
              {formatDate(document.created_at)}
            </span>
          </DetailRow>
          <DetailRow label="SHA-256">
            <span className="flex min-w-0 items-center gap-1">
              <code
                className="truncate font-mono text-2xs text-muted-foreground"
                title={document.sha256}
              >
                {document.sha256.slice(0, 16)}…
              </code>
              <Button
                variant="ghost"
                size="icon-xs"
                aria-label="Copy the SHA-256"
                onClick={copySha}
                className="text-muted-foreground"
              >
                <Icon icon={Copy01Icon} />
              </Button>
            </span>
          </DetailRow>
        </DetailList>
      </section>
      <section className="px-5 py-5">
        <h3 className="mb-4 text-xs font-medium text-foreground">
          Search index
        </h3>
        <DetailList className="mx-0 gap-3.5 border-0 py-0 max-[600px]:mx-0">
          <DetailRow label="State">
            <StatusBadge status={INDEX_STATES[state].status}>
              {INDEX_STATES[state].label}
            </StatusBadge>
          </DetailRow>
          <DetailRow label="Chunks">
            <span
              className={cn("tabular-nums", state !== "ready" && "text-subtle")}
            >
              {state === "ready" ? formatCount(document.chunk_count) : "–"}
            </span>
          </DetailRow>
          {document.finished_at && (
            <DetailRow label={state === "failed" ? "Failed" : "Indexed"}>
              <span title={formatDate(document.finished_at)}>
                {formatWhen(document.finished_at)}
              </span>
            </DetailRow>
          )}
        </DetailList>
        {state === "ready" && (
          <p className="mt-4 text-xs/relaxed text-muted-foreground">
            Agents search its {formatCount(document.chunk_count)}{" "}
            {document.chunk_count === 1 ? "chunk" : "chunks"} when they answer.
          </p>
        )}
        {(state === "indexing" || state === "queued") && (
          <p className="mt-4 text-xs/relaxed text-muted-foreground">
            It becomes searchable when the worker finishes with it.
          </p>
        )}
        {(state === "failed" || state === "unknown") && (
          <div className="mt-4 grid gap-3 rounded-(--radius-card) bg-danger-surface/60 px-3.5 py-3 text-xs/relaxed">
            <p className="text-danger-foreground">
              {state === "failed"
                ? describeFailure(document)
                : "The worker stopped reporting on it. Retry it to be sure it's searchable."}
            </p>
            {document.error && (
              <details className="text-2xs text-muted-foreground">
                <summary className="cursor-pointer select-none">
                  The worker's message
                </summary>
                <p className="mt-1.5 font-mono break-words whitespace-pre-wrap">
                  {document.error}
                </p>
              </details>
            )}
            {canManage && (
              <Button
                variant="outline"
                size="sm"
                className="justify-self-start bg-background"
                onClick={() => onRetry(document)}
              >
                <Icon icon="refresh" data-icon="inline-start" />
                Retry indexing
              </Button>
            )}
          </div>
        )}
      </section>
    </aside>
  )
}
