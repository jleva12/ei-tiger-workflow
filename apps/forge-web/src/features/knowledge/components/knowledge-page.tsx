import * as React from "react"
import { Link } from "@tanstack/react-router"
import { cn } from "cn"
import {
  FileUploadIcon,
  Folder01Icon,
  FolderUploadIcon,
  GridViewIcon,
  Upload04Icon,
} from "@hugeicons/core-free-icons"

import type { RowSelectionState } from "@/components/forge/data-table/index"
import { PrimaryAction } from "@/components/forge/app-shell"
import {
  EmptyIllustration,
  EmptyWorkspace,
  PageEmpty,
  PanelEmpty,
} from "@/components/forge/empty-state"
import { ActiveFilters, ErrorCallout } from "@/components/forge/feedback"
import { Icon } from "@/components/forge/icon"
import { RunStateIcon, StatusSymbol } from "@/components/forge/status"
import { toneVars } from "@/components/forge/variants"
import { ShellHeaderActions, useShellPage } from "@/components/forge/shell"
import { LayoutSwitch, SearchField } from "@/components/forge/toolbar"
import { Button } from "@/components/ui/button"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { Skeleton } from "@/components/ui/skeleton"
import { toast } from "@/components/ui/toast"
import { DeleteDialog } from "@/features/admin/components/delete-dialog"
import { useMyOrganizations } from "@/lib/access"
import { useScopeAccess } from "@/lib/hierarchy"
import { useMe, userName, users, type User } from "@/lib/users"
import {
  documentCollections,
  kindFilter,
  useDocumentSummary,
  useDownloadDocument,
  useKnowledgeBase,
  useKnowledgeDocuments,
  useKnowledgeMutations,
  useRecentDocuments,
  useUploads,
  type BulkResult,
  type DocumentCollection,
  type DocumentFilters,
  type KnowledgeBase,
  type KnowledgeDocument,
  type KnowledgeScope,
} from "../lib/api"
import {
  collectionTree,
  formatCount,
  INDEX_STATES,
  itemOfPickedFile,
  KINDS,
  KNOWLEDGE_ICON,
  readDroppedEntries,
  sortUploadItems,
  SUPPORTED_EXTENSIONS,
  totalsByState,
  UNFILED,
  type KnowledgeSearch,
  type KnowledgeShelf,
  type UploadItem,
} from "../lib/knowledge"
import { CollectionDialog } from "./collection-dialog"
import { DocumentViewer } from "./document-viewer"
import { DocumentsView, type BulkActions } from "./knowledge-documents"
import { KnowledgeNav } from "./knowledge-nav"
import { CollectionShelf, KindStrip } from "./knowledge-overview"
import { KnowledgeRail } from "./knowledge-rail"
import { KnowledgeSearchDialog } from "./knowledge-search"

export type SearchChange = (
  patch: Partial<KnowledgeSearch>,
  options?: { replace?: boolean }
) => void

/**
 * A knowledge base's page: everything uploaded to it for the organization's
 * agents to search, laid out like a drive. Kinds and collections across the
 * top, the documents below (rows or cards), and on the right what the index
 * holds, who added to it and what happened lately. The sidebar swaps to the
 * page's own nav while it's open. Collections nest like folders, and the
 * documents table shows them as a tree. Those who manage knowledge bases
 * (`knowledge_bases:manage`) upload files or whole folders, by button or by
 * dropping them anywhere: a folder's tree becomes collections, reusing those
 * that already have its names. They file documents in collections and
 * remove them; every member reads, opens any document in the viewer over the
 * page (`?document=`) to read the file itself, and tries a search as agents
 * would. Only the organization's members get in.
 */
export function KnowledgePage({
  organizationId,
  knowledgeBaseId,
  search,
  onSearch,
}: {
  organizationId: string
  knowledgeBaseId: string
  search: KnowledgeSearch
  onSearch: SearchChange
}) {
  const myOrganizations = useMyOrganizations()
  const organization = myOrganizations.data?.find(
    (o) => o.id === organizationId
  )
  const base = useKnowledgeBase({ organizationId, knowledgeBaseId })

  useShellPage({
    header: {
      title: base.data?.name ?? "Knowledge base",
      icon: KNOWLEDGE_ICON,
      breadcrumbs: organization
        ? [
            {
              label: organization.name,
              href: `/organizations/${organizationId}`,
            },
            {
              label: "Knowledge bases",
              href: `/organizations/${organizationId}?view=knowledge`,
            },
          ]
        : [],
    },
    // The page draws its own sub nav (below), not the workspace's.
    sidebar: {
      label: base.data ? `${base.data.name} knowledge base` : "Knowledge base",
      sections: [],
    },
  })

  if (myOrganizations.isPending || (organization && base.isPending)) {
    return (
      <div className="flex flex-col gap-3" aria-busy="true">
        <Skeleton className="h-[92px] w-full rounded-(--radius-band)" />
        <Skeleton className="h-4 w-80 max-w-full" />
      </div>
    )
  }
  const failed = myOrganizations.error ?? base.error
  if (failed && !(base.error?.status === 404)) {
    return (
      <ErrorCallout
        title={
          myOrganizations.error
            ? "Couldn't load your organizations"
            : "Couldn't load the knowledge base"
        }
        action={
          <Button
            variant="outline"
            size="sm"
            onClick={() =>
              void (myOrganizations.error
                ? myOrganizations.refetch()
                : base.refetch())
            }
          >
            Retry
          </Button>
        }
      >
        {failed.message}
      </ErrorCallout>
    )
  }
  if (!organization) {
    return (
      <PageEmpty
        illustration="locked"
        title="You're not in this organization"
        description="Only an organization's members can open its knowledge bases. Pick one of your organizations at the top of the sidebar, or ask one of the organization's admins to add you."
      />
    )
  }
  if (!base.data) {
    return (
      <PageEmpty
        illustration="search"
        title="This knowledge base isn't here anymore"
        description="It was deleted, or the link is wrong."
      >
        <Button
          variant="outline"
          nativeButton={false}
          render={
            <Link
              to="/organizations/$organizationId"
              params={{ organizationId }}
              search={{ view: "knowledge" }}
            />
          }
        >
          Back to the knowledge bases
        </Button>
      </PageEmpty>
    )
  }
  return (
    <Knowledge
      organizationId={organizationId}
      base={base.data}
      search={search}
      onSearch={onSearch}
    />
  )
}

// ID → "Ada Lovelace", rebuilt only when the users list changes.
const toNames = (list: User[]) =>
  new Map(list.map((user) => [user.id, userName(user)]))

/** Names a user ID by the person's name; anything else as it is. */
function useNameOf() {
  const { data: names } = users.useList(undefined, { select: toNames })
  return React.useCallback((user: string) => names?.get(user) ?? user, [names])
}

/** Focus the search with "/" when not typing somewhere else. */
function useSlashToSearch(
  input: React.RefObject<HTMLInputElement | null>,
  enabled: boolean
) {
  React.useEffect(() => {
    if (!enabled) return
    function onKeyDown(event: KeyboardEvent) {
      if (event.key !== "/" || event.metaKey || event.ctrlKey || event.altKey)
        return
      const target = event.target as HTMLElement | null
      if (target?.closest("input, textarea, select, [contenteditable='true']"))
        return
      event.preventDefault()
      input.current?.focus()
    }
    window.addEventListener("keydown", onKeyDown)
    return () => window.removeEventListener("keydown", onKeyDown)
  }, [input, enabled])
}

/**
 * Whether files are being dragged over the window, calling `onFiles` with
 * what's dropped: files, and every file in a dropped folder with the folders
 * it was in. Off while `enabled` is false.
 */
function useFileDrop(enabled: boolean, onFiles: (items: UploadItem[]) => void) {
  const [dragging, setDragging] = React.useState(false)
  const latest = React.useRef(onFiles)
  React.useEffect(() => {
    latest.current = onFiles
  })
  React.useEffect(() => {
    if (!enabled) return
    // dragenter and dragleave fire for every element crossed.
    let depth = 0
    const carriesFiles = (event: DragEvent) =>
      event.dataTransfer?.types.includes("Files") ?? false
    const enter = (event: DragEvent) => {
      if (!carriesFiles(event)) return
      event.preventDefault()
      depth += 1
      setDragging(true)
    }
    const over = (event: DragEvent) => {
      if (!carriesFiles(event)) return
      event.preventDefault()
      if (event.dataTransfer) event.dataTransfer.dropEffect = "copy"
    }
    const leave = (event: DragEvent) => {
      if (!carriesFiles(event)) return
      depth = Math.max(0, depth - 1)
      if (depth === 0) setDragging(false)
    }
    const drop = (event: DragEvent) => {
      if (!carriesFiles(event)) return
      event.preventDefault()
      depth = 0
      setDragging(false)
      // The entries are only readable during the event; their files after.
      const entries = [...(event.dataTransfer?.items ?? [])]
        .map((item) => item.webkitGetAsEntry?.())
        .filter((entry): entry is FileSystemEntry => Boolean(entry))
      if (entries.some((entry) => entry.isDirectory)) {
        void readDroppedEntries(entries).then(
          (items) => latest.current(items),
          () =>
            toast.add({
              type: "error",
              title: "Couldn't read the dropped folder",
              description: "Try Upload › Folder instead.",
            })
        )
        return
      }
      latest.current(
        [...(event.dataTransfer?.files ?? [])].map((file) => ({
          file,
          folders: [],
        }))
      )
    }
    window.addEventListener("dragenter", enter)
    window.addEventListener("dragover", over)
    window.addEventListener("dragleave", leave)
    window.addEventListener("drop", drop)
    return () => {
      window.removeEventListener("dragenter", enter)
      window.removeEventListener("dragover", over)
      window.removeEventListener("dragleave", leave)
      window.removeEventListener("drop", drop)
      setDragging(false)
    }
  }, [enabled])
  return dragging
}

const READABLE =
  "PDF, Word, PowerPoint, Excel, OpenDocument, CSV, Markdown, text, Visio or Mermaid"

function Knowledge({
  organizationId,
  base,
  search,
  onSearch,
}: {
  organizationId: string
  base: KnowledgeBase
  search: KnowledgeSearch
  onSearch: SearchChange
}) {
  const knowledgeBaseId = base.id
  const scope = React.useMemo<KnowledgeScope>(
    () => ({ organizationId, knowledgeBaseId }),
    [organizationId, knowledgeBaseId]
  )
  const can = useScopeAccess(`org:${organizationId}`)
  const canManage = can("knowledge_bases:manage")
  const me = useMe().data?.subject
  const nameOf = useNameOf()

  const summary = useDocumentSummary(scope)
  const collections = documentCollections.scope(scope).useList()
  const recent = useRecentDocuments(scope)
  const { uploads, start, dismiss } = useUploads(scope)
  const {
    move,
    retry,
    remove,
    bulk,
    createCollection,
    updateCollection,
    ensurePaths,
    deleteCollection,
  } = useKnowledgeMutations(scope)
  const tree = React.useMemo(
    () => collectionTree(collections.data),
    [collections.data]
  )

  const { shelf, collection, kind, q, document: viewing } = search
  const layout = search.layout ?? "list"
  const openCollection =
    collection && collection !== UNFILED ? tree.byId.get(collection) : undefined
  // A collection lists with every collection in it.
  const place = React.useMemo(
    () =>
      collection === UNFILED
        ? [UNFILED]
        : openCollection
          ? tree.subtreeIds(openCollection.id)
          : collection
            ? [collection]
            : undefined,
    [collection, openCollection, tree]
  )
  // Where you are, as a tree of collections and documents; a shelf, a kind
  // or a search lists what matches instead.
  const treeMode = !shelf && !kind && !q && collection !== UNFILED
  const filters = React.useMemo<DocumentFilters>(
    () => ({
      ...(place && { collection: place }),
      ...(shelf === "indexing" && { phase: ["QUEUED", "RUNNING"] }),
      ...(shelf === "failed" && { phase: ["FAILED"] }),
      ...(shelf === "mine" && me && { uploaded_by: me }),
      ...kindFilter(kind),
      ...(q && { q }),
    }),
    [place, shelf, me, kind, q]
  )
  const documents = useKnowledgeDocuments(scope, filters, {
    // Yours waits for who you are.
    enabled: shelf !== "mine" || me !== undefined,
  })
  const listed = React.useMemo(
    () => documents.data?.pages.flat() ?? [],
    [documents.data]
  )

  const filed = (collections.data ?? []).reduce(
    (sum, each) => sum + each.document_count,
    0
  )
  const unfiled = Math.max(0, (summary.data?.totals.documents ?? 0) - filed)

  // Inside a collection, the top bar walks down to it.
  const organizationName = useMyOrganizations().data?.find(
    (o) => o.id === organizationId
  )?.name
  const basePath = `/organizations/${encodeURIComponent(organizationId)}/knowledge/${encodeURIComponent(knowledgeBaseId)}`
  useShellPage(
    openCollection
      ? {
          header: {
            title: openCollection.name,
            icon: Folder01Icon,
            breadcrumbs: [
              ...(organizationName
                ? [
                    {
                      label: organizationName,
                      href: `/organizations/${organizationId}`,
                    },
                  ]
                : []),
              {
                label: "Knowledge bases",
                href: `/organizations/${organizationId}?view=knowledge`,
              },
              { label: base.name, href: basePath },
              ...tree
                .pathOf(openCollection.id)
                .slice(0, -1)
                .map((above) => ({
                  label: above.name,
                  href: `${basePath}?collection=${encodeURIComponent(above.id)}`,
                })),
            ],
          },
        }
      : {}
  )

  // The search box writes to the URL once typing pauses.
  const searchInput = React.useRef<HTMLInputElement>(null)
  useSlashToSearch(searchInput, !viewing)
  const [query, setQuery] = React.useState(q ?? "")
  const [lastQ, setLastQ] = React.useState(q)
  if (q !== lastQ) {
    setLastQ(q)
    setQuery(q ?? "")
  }
  React.useEffect(() => {
    const next = query.trim() || undefined
    if (next === q) return
    const timer = window.setTimeout(
      () => onSearch({ q: next }, { replace: true }),
      250
    )
    return () => window.clearTimeout(timer)
  }, [query, q, onSearch])

  // Uploads land in the open collection, if one is; a folder's tree becomes
  // collections in it, reusing those that already have its names.
  const fileInput = React.useRef<HTMLInputElement>(null)
  const folderInput = React.useRef<HTMLInputElement>(null)
  const target = openCollection ?? null
  const upload = async (items: UploadItem[]) => {
    const { readable, skipped } = sortUploadItems(items)
    if (skipped.length > 0)
      toast.add({
        type: "warning",
        title:
          skipped.length === 1
            ? `Skipped ${skipped[0]?.file.name}`
            : `Skipped ${skipped.length} files`,
        description: `Agents can read ${READABLE} files.`,
      })
    if (readable.length === 0) return
    const parentId = target?.id ?? null
    const key = (folders: string[]) => folders.join("\u0000")
    const paths = [
      ...new Map(
        readable
          .filter((item) => item.folders.length > 0)
          .map((item) => [key(item.folders), item.folders])
      ).values(),
    ]
    const placed = new Map<string, string>()
    if (paths.length > 0) {
      if (paths.length > 1000) {
        toast.add({
          type: "error",
          title: "That folder has too many folders in it",
          description:
            "Upload at most 1,000 folders at a time: pick the folders inside it one by one.",
        })
        return
      }
      try {
        const made = await ensurePaths.mutateAsync({ parentId, paths })
        for (const { path, collection } of made.collections)
          placed.set(key(path), collection.id)
      } catch {
        // The mutation's error toast says why.
        return
      }
      const tops = [...new Set(paths.map((path) => path[0]))]
      toast.add({
        type: "info",
        title: `Uploading ${readable.length.toLocaleString()} ${readable.length === 1 ? "file" : "files"} from ${tops.length === 1 ? tops[0] : `${tops.length} folders`}`,
        description: `Each folder is a collection${target ? ` in ${target.name}` : ""}.`,
      })
    }
    start(
      readable.map(({ file, folders }) => ({
        file,
        collectionId:
          folders.length > 0
            ? (placed.get(key(folders)) ?? parentId)
            : parentId,
      }))
    )
  }
  // Not while a document is open over the page.
  const dragging = useFileDrop(
    canManage && !viewing,
    (items) => void upload(items)
  )
  const pickFiles = () => fileInput.current?.click()
  const pickFolder = () => folderInput.current?.click()

  const [editing, setEditing] = React.useState<{
    open: boolean
    /** The one to rename; none to create one. */
    collection?: DocumentCollection
    /** Where a new one goes; the top of the knowledge base when null. */
    parent?: DocumentCollection | null
  }>({ open: false })
  const newCollection = React.useCallback(
    (parent: DocumentCollection | null) => setEditing({ open: true, parent }),
    []
  )
  const [testing, setTesting] = React.useState(false)
  const [removing, setRemoving] = React.useState<KnowledgeDocument>()
  const [deleting, setDeleting] = React.useState<DocumentCollection>()

  // Stable, so the table's rows don't redraw on every render.
  const moveMutate = move.mutate
  const collectionList = collections.data
  const moveDocument = React.useCallback(
    (document: KnowledgeDocument, collectionId: string | null) =>
      moveMutate(
        { id: document.id, collectionId },
        {
          onSuccess: () =>
            toast.add({
              type: "success",
              title: collectionId
                ? `Moved to ${collectionList?.find((each) => each.id === collectionId)?.name ?? "the collection"}`
                : "Unfiled",
              description: document.filename,
            }),
        }
      ),
    [moveMutate, collectionList]
  )
  // The documents ticked to act on them all. Somewhere else (another
  // collection, shelf, kind, search or layout) starts with none.
  const [selected, setSelected] = React.useState<RowSelectionState>({})
  const selectionPlace = `${JSON.stringify(filters)}|${layout}`
  const [lastSelectionPlace, setLastSelectionPlace] =
    React.useState(selectionPlace)
  if (selectionPlace !== lastSelectionPlace) {
    setLastSelectionPlace(selectionPlace)
    setSelected({})
  }
  const [removingAll, setRemovingAll] = React.useState<KnowledgeDocument[]>()
  // Those it couldn't act on stay ticked, to try again.
  const keepFailed = ({ failed }: BulkResult) =>
    setSelected(
      Object.fromEntries(failed.map(({ document }) => [document.id, true]))
    )
  const reportFailed = (verb: string, result: BulkResult, total: number) => {
    const [first] = result.failed
    if (!first) return
    toast.add({
      type: "error",
      title: `Couldn't ${verb} ${formatCount(result.failed.length)} of ${documentsText(total)}`,
      description: `${first.document.filename}: ${first.error.message}${result.failed.length > 1 ? ` (and ${formatCount(result.failed.length - 1)} more)` : ""}. They're still selected.`,
    })
  }
  const retryAll = async (picked: KnowledgeDocument[]) => {
    const result = await bulk.mutateAsync({
      action: "retry",
      documents: picked,
    })
    keepFailed(result)
    if (result.done > 0)
      toast.add({
        type: "success",
        title: `Queued ${documentsText(result.done)} for indexing`,
      })
    reportFailed("retry", result, picked.length)
  }
  const moveAll = async (
    picked: KnowledgeDocument[],
    collectionId: string | null
  ) => {
    const result = await bulk.mutateAsync({
      action: "move",
      collectionId,
      documents: picked,
    })
    keepFailed(result)
    if (result.done > 0)
      toast.add({
        type: "success",
        title: collectionId
          ? `Moved ${documentsText(result.done)} to ${tree.byId.get(collectionId)?.name ?? "the collection"}`
          : `Unfiled ${documentsText(result.done)}`,
      })
    reportFailed("move", result, picked.length)
  }
  const bulkActions: BulkActions = {
    selected,
    onSelectedChange: setSelected,
    busy: bulk.isPending,
    onRetry: (picked) => void retryAll(picked),
    onMove: (picked, collectionId) => void moveAll(picked, collectionId),
    onRemove: setRemovingAll,
  }

  // Opening one pushes it on the history, so Back closes it; paging through
  // them and closing don't.
  const openDocument = React.useCallback(
    (document: KnowledgeDocument) => onSearch({ document: document.id }),
    [onSearch]
  )
  const closeDocument = () =>
    onSearch({ document: undefined }, { replace: true })
  const download = useDownloadDocument(scope)
  const downloadMutate = download.mutate
  const downloadDocument = React.useCallback(
    (document: KnowledgeDocument) => downloadMutate(document),
    [downloadMutate]
  )

  const retryMutate = retry.mutate
  const retryDocument = React.useCallback(
    (document: KnowledgeDocument) =>
      retryMutate(document.id, {
        onSuccess: () =>
          toast.add({
            type: "success",
            title: "Queued for indexing",
            description: document.filename,
          }),
      }),
    [retryMutate]
  )

  const states = totalsByState(summary.data)
  const mine = summary.data?.contributors.find((who) => who.user === me)
  const heading =
    collection === UNFILED
      ? "Unfiled"
      : openCollection
        ? tree.pathName(openCollection.id)
        : shelf === "indexing"
          ? "Indexing"
          : shelf === "failed"
            ? "Failed"
            : shelf === "mine"
              ? "Uploaded by you"
              : "All documents"
  // The place's own count; with a kind or search on, the list says less.
  const count =
    kind || q
      ? undefined
      : collection === UNFILED
        ? unfiled
        : openCollection
          ? tree.totals(openCollection.id).documents
          : shelf === "indexing"
            ? states.queued.documents + states.indexing.documents
            : shelf === "failed"
              ? states.failed.documents
              : shelf === "mine"
                ? (mine?.documents ?? 0)
                : summary.data?.totals.documents

  const firstRun =
    summary.data?.totals.documents === 0 &&
    uploads.length === 0 &&
    !collection &&
    !shelf
  const filtered = Boolean(kind || q || shelf || collection)

  return (
    <>
      <KnowledgeNav
        scope={scope}
        place={{ shelf, collection }}
        summary={summary.data}
        collections={collections.data}
        tree={tree}
        unfiled={unfiled}
        canManage={canManage}
        onNewCollection={() => newCollection(openCollection ?? null)}
      />
      <ShellHeaderActions>
        <SearchField
          ref={searchInput}
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="Search documents"
          aria-label="Search documents by name"
          // No keyboard shortcut to hint at on a phone.
          className="@max-[600px]/shell:[&_kbd]:hidden"
        />
        <Button
          variant="outline"
          onClick={() => setTesting(true)}
          title="Search the knowledge base as an agent would"
        >
          <Icon icon="sparkles" data-icon="inline-start" />
          <span className="@max-[600px]/shell:sr-only">Test search</span>
        </Button>
        <LayoutSwitch
          value={layout}
          onValueChange={(value) =>
            onSearch(
              { layout: value === "grid" ? "grid" : undefined },
              { replace: true }
            )
          }
          options={[
            { value: "list", label: "List", icon: "list" },
            { value: "grid", label: "Grid", icon: GridViewIcon },
          ]}
        />
        {canManage && (
          <DropdownMenu>
            <DropdownMenuTrigger render={<PrimaryAction icon={Upload04Icon} />}>
              Upload
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" className="min-w-52">
              <DropdownMenuItem onClick={pickFiles}>
                <Icon icon={FileUploadIcon} />
                Upload files…
              </DropdownMenuItem>
              <DropdownMenuItem onClick={pickFolder}>
                <Icon icon={FolderUploadIcon} />
                <span className="grid">
                  Upload a folder…
                  <span className="text-2xs text-muted-foreground">
                    Its folders become collections
                  </span>
                </span>
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        )}
      </ShellHeaderActions>
      {canManage && (
        <>
          <input
            ref={fileInput}
            type="file"
            multiple
            hidden
            accept={SUPPORTED_EXTENSIONS.map(
              (extension) => `.${extension}`
            ).join(",")}
            onChange={(event) => {
              void upload(
                [...(event.target.files ?? [])].map((file) => ({
                  file,
                  folders: [],
                }))
              )
              event.target.value = ""
            }}
          />
          <input
            // A folder picker: the browser asks for a folder and hands over
            // every file in it, each with its path.
            ref={(input) => {
              folderInput.current = input
              if (input) input.webkitdirectory = true
            }}
            type="file"
            multiple
            hidden
            onChange={(event) => {
              void upload([...(event.target.files ?? [])].map(itemOfPickedFile))
              event.target.value = ""
            }}
          />
        </>
      )}

      {summary.error && (
        <ErrorCallout
          className="mb-6"
          title="Couldn't add up the knowledge base's documents"
          action={
            <Button
              variant="outline"
              size="sm"
              onClick={() => void summary.refetch()}
            >
              Retry
            </Button>
          }
        >
          {summary.error.message}
        </ErrorCallout>
      )}

      {firstRun ? (
        <EmptyWorkspace
          illustration={<EmptyIllustration name="tasks" />}
          title="Give the agents something to read"
          description={
            canManage
              ? `Upload runbooks, specs and reference sheets to ${base.name}, or a whole folder of them, or drop them anywhere on this page. A folder's folders become collections. Each file is split into chunks and embedded so the organization's agents can search it.`
              : `Nothing has been uploaded to ${base.name} yet. Those who manage the organization's knowledge bases upload the documents its agents search.`
          }
          actions={
            canManage ? (
              <>
                <Button variant="outline" onClick={pickFiles}>
                  <Icon icon={FileUploadIcon} data-icon="inline-start" />
                  Choose files
                </Button>
                <Button variant="ghost" onClick={pickFolder}>
                  <Icon icon={FolderUploadIcon} data-icon="inline-start" />
                  Choose a folder
                </Button>
              </>
            ) : undefined
          }
          steps={[
            { icon: Upload04Icon, label: "Upload" },
            { icon: "loading", label: "Indexed" },
            { icon: "search", label: "Searchable" },
          ]}
        />
      ) : (
        // With the rail beside it, exactly as tall as the page, which then
        // never scrolls: the documents fill what the kinds and collections
        // leave, their rows scrolling, and the rail scrolls on its own. Its
        // hairline runs from the top bar to the footer. With the rail below
        // (narrower), the page scrolls as usual. The documents keep 18rem on
        // a short screen, where the page scrolls too.
        <div className="grid h-full grid-cols-[minmax(0,1fr)_17.5rem] grid-rows-[minmax(0,1fr)] gap-x-8 @max-[1270px]/shell:h-auto @max-[1270px]/shell:grid-cols-1 @max-[1270px]/shell:grid-rows-none @min-[1700px]/shell:grid-cols-[minmax(0,1fr)_19rem]">
          <div className="flex min-h-0 min-w-0 flex-col @max-[1270px]/shell:block">
            <KindStrip
              summary={summary.data}
              kind={kind}
              onKindChange={(next) => onSearch({ kind: next })}
            />
            <CollectionShelf
              scope={scope}
              // Those at this level: the top ones, or the open one's.
              collections={
                collections.data &&
                (collection === UNFILED
                  ? []
                  : tree.childrenOf(openCollection?.id ?? null))
              }
              totals={tree.totals}
              unfiled={unfiled}
              inside={openCollection}
              selected={collection}
              canManage={canManage}
              onNew={() => newCollection(openCollection ?? null)}
              onRename={(each) => setEditing({ open: true, collection: each })}
              onDelete={setDeleting}
            />
            <section
              aria-label="Documents"
              className="mt-8 flex min-h-72 flex-1 flex-col @max-[1270px]/shell:block"
            >
              {!shelf && !collection && !kind && !q && (
                <StateBands
                  scope={scope}
                  failed={states.failed.documents}
                  unfinished={
                    states.queued.documents + states.indexing.documents
                  }
                />
              )}
              {(kind || q) && (
                <ActiveFilters
                  className="mb-3"
                  onClear={() => {
                    setQuery("")
                    onSearch({ kind: undefined, q: undefined })
                  }}
                >
                  {[kind && KINDS[kind].label, q && `names containing “${q}”`]
                    .filter(Boolean)
                    .join(" · ")}
                </ActiveFilters>
              )}
              {documents.error ? (
                <ErrorCallout
                  title="Couldn't load the documents"
                  action={
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() => void documents.refetch()}
                    >
                      Retry
                    </Button>
                  }
                >
                  {documents.error.message}
                </ErrorCallout>
              ) : (
                <DocumentsView
                  scope={scope}
                  layout={layout}
                  heading={heading}
                  count={count}
                  documents={listed}
                  isLoading={documents.isPending}
                  empty={
                    <PanelEmpty illustration={filtered ? "search" : "tasks"}>
                      {emptyLine({ shelf, collection, kind, q })}
                      {(kind || q) && (
                        <Button
                          variant="outline"
                          size="sm"
                          className="mt-3"
                          onClick={() => {
                            setQuery("")
                            onSearch({ kind: undefined, q: undefined })
                          }}
                        >
                          Clear filters
                        </Button>
                      )}
                    </PanelEmpty>
                  }
                  uploads={uploads}
                  onDismissUpload={dismiss}
                  showCollection={!collection}
                  tree={tree}
                  treeMode={treeMode}
                  location={openCollection?.id ?? null}
                  onOpenCollection={(id) =>
                    onSearch({ shelf: undefined, collection: id })
                  }
                  onNewCollection={newCollection}
                  onRenameCollection={(each) =>
                    setEditing({ open: true, collection: each })
                  }
                  onDeleteCollection={setDeleting}
                  nameOf={nameOf}
                  me={me}
                  canManage={canManage}
                  collections={collections.data}
                  onOpen={openDocument}
                  onDownload={downloadDocument}
                  onMove={moveDocument}
                  onRetry={retryDocument}
                  onRemove={setRemoving}
                  bulk={bulkActions}
                  hasMore={documents.hasNextPage}
                  loadingMore={documents.isFetchingNextPage}
                  onLoadMore={() => void documents.fetchNextPage()}
                />
              )}
            </section>
          </div>
          <KnowledgeRail
            summary={summary.data}
            recent={recent.data}
            nameOf={nameOf}
            me={me}
            onOpen={openDocument}
            // Out through the page's own top and bottom padding (PageContent),
            // so the hairline meets the top bar and the footer.
            className="-mt-5 -mb-[26px] min-h-0 overflow-y-auto border-l pt-5 pb-[26px] pl-7 @max-[1270px]/shell:mt-10 @max-[1270px]/shell:mb-0 @max-[1270px]/shell:overflow-visible @max-[1270px]/shell:border-t @max-[1270px]/shell:border-l-0 @max-[1270px]/shell:pt-6 @max-[1270px]/shell:pb-0 @max-[1270px]/shell:pl-0"
          />
        </div>
      )}

      {viewing && (
        <DocumentViewer
          scope={scope}
          documentId={viewing}
          documents={listed}
          tree={tree}
          collections={collections.data}
          canManage={canManage}
          nameOf={nameOf}
          me={me}
          onOpen={(id) => onSearch({ document: id }, { replace: true })}
          onClose={closeDocument}
          onMove={moveDocument}
          onRetry={retryDocument}
          onRemove={setRemoving}
        />
      )}
      {dragging && (
        <DropVeil
          into={target ? tree.pathName(target.id) : base.name}
          filed={target !== null}
        />
      )}
      <KnowledgeSearchDialog
        open={testing}
        onOpenChange={setTesting}
        scope={scope}
        baseName={base.name}
        onOpenDocument={(id) => onSearch({ document: id })}
      />
      <CollectionDialog
        open={editing.open}
        onOpenChange={(open) => setEditing((current) => ({ ...current, open }))}
        collection={editing.collection}
        parent={editing.parent ?? null}
        baseName={base.name}
        onSubmit={(input) =>
          editing.collection
            ? updateCollection.mutateAsync({ id: editing.collection.id, input })
            : createCollection.mutateAsync({
                ...input,
                parent_id: editing.parent?.id ?? null,
              })
        }
      />
      <DeleteDialog
        open={removing !== undefined}
        onClose={() => setRemoving(undefined)}
        onConfirm={async () => {
          const { id } = removing!
          // The viewer moves on to the next document, or closes.
          const at = listed.findIndex((each) => each.id === id)
          const after = listed[at + 1] ?? listed[at - 1]
          await remove.mutateAsync(id)
          if (viewing === id)
            onSearch({ document: after?.id }, { replace: true })
        }}
        title={`Remove ${removing?.filename ?? "the document"}?`}
        description={
          removing?.phase === "SUCCEEDED"
            ? `Agents stop finding it: its file and its ${removing.chunk_count.toLocaleString()} ${removing.chunk_count === 1 ? "chunk are" : "chunks are"} deleted. This can't be undone.`
            : "Its file is deleted, and anything the worker embedded from it. This can't be undone."
        }
        confirmLabel="Remove document"
      />
      <DeleteDialog
        open={removingAll !== undefined}
        onClose={() => setRemovingAll(undefined)}
        onConfirm={async () => {
          const all = removingAll ?? []
          const result = await bulk.mutateAsync({
            action: "remove",
            documents: all,
          })
          keepFailed(result)
          const [first] = result.failed
          if (first) {
            // The dialog stays open on what's left, saying why.
            setRemovingAll(result.failed.map(({ document }) => document))
            throw new Error(
              `${result.done > 0 ? `Removed ${formatCount(result.done)}; c` : "C"}ouldn't remove ${formatCount(result.failed.length)}. ${first.document.filename}: ${first.error.message}`
            )
          }
          toast.add({
            type: "success",
            title: `Removed ${documentsText(result.done)}`,
          })
        }}
        title={
          removingAll?.length === 1
            ? `Remove ${removingAll[0].filename}?`
            : `Remove ${documentsText(removingAll?.length ?? 0)}?`
        }
        description={removeAllLine(removingAll ?? [])}
        confirmLabel={
          removingAll?.length === 1
            ? "Remove document"
            : `Remove ${documentsText(removingAll?.length ?? 0)}`
        }
      />
      <DeleteDialog
        open={deleting !== undefined}
        onClose={() => setDeleting(undefined)}
        onConfirm={async () => {
          const { id, parent_id } = deleting!
          await deleteCollection.mutateAsync(id)
          // Where you were is gone: go to where its contents went.
          if (place?.includes(id))
            onSearch({ collection: parent_id ?? undefined })
        }}
        title={`Delete ${deleting?.name ?? "the collection"}?`}
        description={deleteCollectionLine(deleting, tree, base.name)}
        confirmLabel="Delete collection"
      />
    </>
  )
}

/**
 * Forge's status bands over the documents, while anything needs a look:
 * what failed to index (agents can't search it), and what's still
 * being indexed. Each opens its shelf.
 */
function StateBands({
  scope,
  failed,
  unfinished,
}: {
  scope: KnowledgeScope
  failed: number
  unfinished: number
}) {
  if (!failed && !unfinished) return null
  const band = (
    tone: "red" | "amber",
    symbol: "failed" | "progress",
    label: string,
    detail: string,
    shelf: KnowledgeShelf
  ) => (
    <div
      className={cn(
        toneVars[tone],
        "flex min-h-[39px] items-center gap-2.5 rounded-(--radius-band) bg-(--tone) py-1.5 pr-1.5 pl-[17px] text-xs"
      )}
    >
      {symbol === "failed" ? (
        // The dashed ring is queued work's; a failure gets the run signal.
        <RunStateIcon state="failed" size={15} />
      ) : (
        <StatusSymbol kind={symbol} />
      )}
      <span className="font-medium text-foreground">{label}</span>
      <span className="truncate text-muted-foreground @max-[800px]/shell:hidden">
        {detail}
      </span>
      <Button
        variant="ghost"
        size="xs"
        className="ml-auto shrink-0"
        nativeButton={false}
        render={
          <Link
            to="/organizations/$organizationId/knowledge/$knowledgeBaseId"
            params={scope}
            search={(prev) => ({ ...prev, shelf, collection: undefined })}
          />
        }
      >
        Review
        <Icon icon="right" data-icon="inline-end" />
      </Button>
    </div>
  )
  const documents = (count: number) =>
    `${count.toLocaleString()} ${count === 1 ? "document" : "documents"}`
  return (
    <div className="mb-3 grid gap-2">
      {failed > 0 &&
        band(
          "red",
          "failed",
          `${documents(failed)} couldn't be indexed`,
          "Agents can't search them until they're retried or uploaded again.",
          "failed"
        )}
      {unfinished > 0 &&
        band(
          "amber",
          "progress",
          `${documents(unfinished)} being indexed`,
          "They become searchable as the worker finishes each one.",
          "indexing"
        )}
    </div>
  )
}

/** What deleting a collection does with what's in it. */
function deleteCollectionLine(
  collection: DocumentCollection | undefined,
  tree: ReturnType<typeof collectionTree>,
  baseName: string
) {
  if (!collection) return ""
  const documents = collection.document_count
  const inside = tree.childrenOf(collection.id).length
  if (!documents && !inside) return "It's empty. Nothing else changes."
  const parent = collection.parent_id
    ? tree.byId.get(collection.parent_id)
    : undefined
  const what = [
    documents &&
      `${documents.toLocaleString()} ${documents === 1 ? "document" : "documents"}`,
    inside && `${inside} ${inside === 1 ? "collection" : "collections"}`,
  ]
    .filter(Boolean)
    .join(" and ")
  return parent
    ? `Its ${what} move up to ${parent.name}. Agents keep searching every document.`
    : `Its ${what} move up to the top of ${baseName}${documents ? " (the documents unfiled)" : ""}. Agents keep searching every document.`
}

const documentsText = (count: number) =>
  `${formatCount(count)} ${count === 1 ? "document" : "documents"}`

/** What removing several documents deletes. */
function removeAllLine(documents: KnowledgeDocument[]) {
  const chunks = documents.reduce(
    (sum, document) =>
      sum + (document.phase === "SUCCEEDED" ? document.chunk_count : 0),
    0
  )
  const them = documents.length === 1 ? "it" : "them"
  const their = documents.length === 1 ? "its" : "their"
  return chunks > 0
    ? `Agents stop finding ${them}: ${their} ${documents.length === 1 ? "file" : "files"} and ${formatCount(chunks)} ${chunks === 1 ? "chunk are" : "chunks are"} deleted. This can't be undone.`
    : `${documents.length === 1 ? "Its file is" : "Their files are"} deleted, and anything the worker embedded from ${them}. This can't be undone.`
}

function emptyLine({ shelf, collection, kind, q }: KnowledgeSearch) {
  if (q) return `No documents have “${q}” in their name.`
  if (kind) return `No ${KINDS[kind].label.toLowerCase()} documents here.`
  if (shelf === "indexing") return "Nothing is waiting to be indexed."
  if (shelf === "failed") return "Nothing failed to index."
  if (shelf === "mine") return "You haven't uploaded any documents."
  if (collection === UNFILED) return "Every document is in a collection."
  if (collection) return "Nothing is filed in this collection yet."
  return "No documents yet."
}

/**
 * What the page shows while files are dragged over it: where they'll land
 * and what agents can read. Pointer only; the Upload button is the
 * way in from the keyboard.
 */
function DropVeil({ into, filed }: { into: string; filed: boolean }) {
  return (
    <div
      aria-hidden="true"
      className="pointer-events-none fixed inset-3 z-50 flex animate-in items-center justify-center rounded-(--radius-dialog) border-2 border-dashed border-foreground/25 bg-background/92 duration-150 fade-in-0 motion-reduce:animate-none"
    >
      <div className="flex max-w-[22rem] flex-col items-center gap-3 text-center">
        <span className="flex size-11 items-center justify-center rounded-full bg-muted text-foreground">
          <Icon icon={Upload04Icon} size={20} />
        </span>
        <p className="text-[1.0625rem] font-medium tracking-[-.2px] text-foreground">
          Drop to upload into {into}
        </p>
        <p className="text-xs text-muted-foreground">
          {filed ? "Filed in this collection. " : ""}A folder's folders become
          collections. Agents can read {READABLE} files; anything else is
          skipped.
        </p>
        <p className="mt-1 flex items-center gap-1.5 text-2xs text-subtle">
          <span
            aria-hidden="true"
            className={`size-2 rounded-[2px] ${INDEX_STATES.queued.bar}`}
          />
          Each file is queued, then indexed
        </p>
      </div>
    </div>
  )
}
