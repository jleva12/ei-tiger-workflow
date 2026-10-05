import * as React from "react"
import { Link } from "@tanstack/react-router"
import {
  Delete02Icon,
  Folder01Icon,
  FolderAddIcon,
  FolderExportIcon,
  FolderOpenIcon,
  PencilEdit02Icon,
} from "@hugeicons/core-free-icons"
import { cn } from "cn"

import {
  ADMIN_TABLE_FEATURES,
  PIN_ACTIONS,
} from "@/features/admin/components/table-config"
import {
  createColumnHelper,
  DataTable,
  type DataTableFeatureConfig,
  type DataTableOptions,
  type InitialTableState,
  type RowSelectionState,
} from "@/components/forge/data-table"
import { LoadMore } from "@/components/forge/feedback"
import { Icon } from "@/components/forge/icon"
import { StatusBadge } from "@/components/forge/status"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
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
import { Spinner } from "@/components/ui/spinner"
import type {
  DocumentCollection,
  KnowledgeDocument,
  KnowledgeScope,
  Upload,
} from "../lib/api"
import {
  canRetry,
  collectionChoices,
  formatBytes,
  formatCount,
  formatDate,
  formatWhen,
  describeFailure,
  INDEX_STATES,
  KINDS,
  kindOf,
  stateOf,
  type CollectionTree,
} from "../lib/knowledge"

type Actions = {
  /** May move, retry and remove documents (`knowledge_bases:manage`). */
  canManage: boolean
  collections: DocumentCollection[] | undefined
  tree: CollectionTree
  /** Shows the document in the viewer. */
  onOpen: (document: KnowledgeDocument) => void
  /** Saves its file to the person's computer. */
  onDownload: (document: KnowledgeDocument) => void
  onMove: (document: KnowledgeDocument, collectionId: string | null) => void
  /** Index a failed (or lost) document again. */
  onRetry: (document: KnowledgeDocument) => void
  onRemove: (document: KnowledgeDocument) => void
}

/**
 * Picking documents to act on them all (a manager's): retry the failed ones'
 * indexing, move them to a collection, or remove them.
 */
export type BulkActions = {
  /** The picked documents' IDs; only listed ones count. */
  selected: RowSelectionState
  onSelectedChange: (selected: RowSelectionState) => void
  /** An action on them is under way. */
  busy: boolean
  onRetry: (documents: KnowledgeDocument[]) => void
  onMove: (documents: KnowledgeDocument[], collectionId: string | null) => void
  onRemove: (documents: KnowledgeDocument[]) => void
}

type CollectionActions = {
  onOpenCollection: (id: string) => void
  onNewCollection: (parent: DocumentCollection | null) => void
  onRenameCollection: (collection: DocumentCollection) => void
  onDeleteCollection: (collection: DocumentCollection) => void
}

export type DocumentsViewProps = Actions &
  CollectionActions & {
    scope: KnowledgeScope
    layout: "list" | "grid"
    /** What the list is: "All documents", a collection's path, a shelf. */
    heading: string
    /** How many there are, when the page knows. */
    count: number | undefined
    documents: KnowledgeDocument[]
    isLoading: boolean
    /** Shown instead of the documents when there are none. */
    empty: React.ReactNode
    uploads: Upload[]
    onDismissUpload: (id: string) => void
    /** Name the collection under each card (when showing them all). */
    showCollection: boolean
    /**
     * Show where you are as a tree: its collections as folder rows holding
     * their collections and documents. Otherwise a flat list of what matches.
     */
    treeMode: boolean
    /** The open collection; the top of the knowledge base when null. */
    location: string | null
    /** Who a user ID is, by name. */
    nameOf: (user: string) => string
    /** Your user ID, to say "You". */
    me: string | undefined
    hasMore: boolean
    loadingMore: boolean
    onLoadMore: () => void
    bulk: BulkActions
  }

/**
 * The documents: in the shared data table, or as cards (the grid layout, and
 * every layout on phones, where the table is too wide). Where you are shows
 * as a tree, like a file explorer: each collection a folder row you open to
 * see the collections and documents in it. Each document shows its kind,
 * index state (and why it failed), size, chunks, and who added it when.
 * Uploads still on their way show with their progress, in their folder. A
 * document's name (or a double-click on its row or card) opens it in the
 * viewer, and its menu opens or downloads it. A manager's menu also retries a
 * failed document's indexing, moves a document between collections or
 * removes it; and makes, renames or deletes a collection. A manager also ticks documents (a row's or a card's checkbox, or
 * all of them) to do the same to them all from the bar that floats at the
 * foot of the list. It fills the height the page leaves it (a flex item),
 * so the page never scrolls: the table's rows scroll under its sticky column
 * headers above its status bar, or the cards scroll under their heading.
 * More documents load from the server a page at a time: from Load more (in
 * the table's status bar, after the cards), or as the rows or cards scroll
 * near their end.
 */
export function DocumentsView({
  scope,
  layout,
  heading,
  count,
  documents,
  isLoading,
  empty,
  uploads,
  onDismissUpload,
  showCollection,
  treeMode,
  location,
  hasMore,
  loadingMore,
  onLoadMore,
  canManage,
  collections,
  tree,
  onOpen,
  onDownload,
  onMove,
  onRetry,
  onRemove,
  onOpenCollection,
  onNewCollection,
  onRenameCollection,
  onDeleteCollection,
  nameOf,
  me,
  bulk,
}: DocumentsViewProps) {
  const { selected, onSelectedChange } = bulk
  // Only what's listed counts, so nothing hidden is acted on.
  const picked = canManage
    ? documents.filter((document) => selected[document.id])
    : []
  const pick = React.useCallback(
    (ids: string[], checked: boolean) => {
      const next = { ...selected }
      for (const id of ids) {
        if (checked) next[id] = true
        else delete next[id]
      }
      onSelectedChange(next)
    },
    [selected, onSelectedChange]
  )
  // The table ticks documents only, never a folder or an upload.
  const tableOptions = React.useMemo<DataTableOptions<DocumentRow>>(
    () => ({
      state: { rowSelection: selected },
      onRowSelectionChange: (updater) =>
        onSelectedChange(
          typeof updater === "function" ? updater(selected) : updater
        ),
      enableRowSelection: (row) => row.original.document !== undefined,
    }),
    [selected, onSelectedChange]
  )
  // Scrolling the table's rows, or the cards, near their end loads the next
  // page, as Load more does. Scroll doesn't bubble, so this listens as it's
  // captured.
  const loadNearEnd = (event: React.UIEvent<HTMLElement>) => {
    const region = event.target as HTMLElement
    if (!SCROLL_REGIONS.has(region.dataset.slot ?? "")) return
    if (!hasMore || loadingMore) return
    if (
      region.scrollHeight - region.scrollTop - region.clientHeight <
      LOAD_AHEAD_PX
    )
      onLoadMore()
  }
  const detail = (document: KnowledgeDocument) =>
    showCollection && document.collection_id
      ? tree.pathName(document.collection_id)
      : undefined
  const props = {
    canManage,
    collections,
    tree,
    onOpen,
    onDownload,
    onMove,
    onRetry,
    onRemove,
    nameOf,
    me,
  }
  const rows = React.useMemo(
    () =>
      treeMode
        ? toTree(uploads, documents, tree, location, nameOf, me)
        : toRows(uploads, documents, tree, nameOf, me),
    [treeMode, uploads, documents, tree, location, nameOf, me]
  )
  const actions = React.useMemo(
    () => ({
      scope,
      canManage,
      collections,
      tree,
      onOpen,
      onDownload,
      onMove,
      onRetry,
      onRemove,
      onDismissUpload,
      onOpenCollection,
      onNewCollection,
      onRenameCollection,
      onDeleteCollection,
    }),
    [
      scope,
      canManage,
      collections,
      tree,
      onOpen,
      onDownload,
      onMove,
      onRetry,
      onRemove,
      onDismissUpload,
      onOpenCollection,
      onNewCollection,
      onRenameCollection,
      onDeleteCollection,
    ]
  )
  const none = !isLoading && rows.length === 0
  const countText =
    count === undefined
      ? undefined
      : `${formatCount(count)} ${count === 1 ? "document" : "documents"}`
  // Cards are the documents here; the collections above are the folders.
  const here = (collectionId: string | null) =>
    !treeMode ||
    (collectionId && tree.byId.has(collectionId) ? collectionId : null) ===
      location
  const cardUploads = uploads.filter((upload) => here(upload.collection_id))
  const cardDocuments = documents.filter((document) =>
    here(document.collection_id)
  )
  const cardsPicked = cardDocuments.filter((document) => selected[document.id])

  const cards = (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="mb-3 flex min-h-7 shrink-0 items-baseline gap-2">
        {canManage && cardDocuments.length > 0 && (
          <Checkbox
            aria-label="Select all documents"
            className="self-center"
            checked={cardsPicked.length === cardDocuments.length}
            indeterminate={
              cardsPicked.length > 0 &&
              cardsPicked.length < cardDocuments.length
            }
            onCheckedChange={(checked) =>
              pick(
                cardDocuments.map((document) => document.id),
                checked
              )
            }
          />
        )}
        <h2 className="text-[0.8125rem] font-medium text-muted-foreground">
          {heading}
        </h2>
        {count !== undefined && (
          <span className="text-2xs text-subtle tabular-nums">
            {formatCount(count)}
          </span>
        )}
      </div>
      {/* The cards scroll here when the page fills the screen. */}
      <div
        data-slot="document-cards"
        tabIndex={0}
        role="region"
        aria-label={heading}
        className="min-h-0 flex-1 overflow-y-auto rounded-(--radius-card)"
      >
        {isLoading ? (
          <DocumentsSkeleton />
        ) : cardUploads.length + cardDocuments.length === 0 ? (
          <div className="rounded-(--radius-card) border border-dashed">
            {empty}
          </div>
        ) : (
          <ul className="grid grid-cols-[repeat(auto-fill,minmax(200px,1fr))] gap-3 @max-[600px]/shell:grid-cols-1 @max-[600px]/shell:gap-2">
            {cardUploads.map((upload) => (
              <UploadCard
                key={upload.id}
                upload={upload}
                onDismiss={onDismissUpload}
              />
            ))}
            {cardDocuments.map((document) => (
              <DocumentCard
                key={document.id}
                document={document}
                collection={detail(document)}
                selected={
                  canManage ? Boolean(selected[document.id]) : undefined
                }
                onSelect={(checked) => pick([document.id], checked)}
                {...props}
              />
            ))}
          </ul>
        )}
        {hasMore && (
          <LoadMore
            label={loadingMore ? "Loading…" : "Load more documents"}
            disabled={loadingMore}
            onClick={onLoadMore}
          />
        )}
      </div>
    </div>
  )

  return (
    // Fills what the page leaves it where the page fits the screen; there,
    // the table's rows (or the cards) scroll and the page doesn't.
    <div
      className="relative flex min-h-0 flex-1 flex-col"
      onScrollCapture={loadNearEnd}
    >
      {layout === "list" ? (
        <>
          <div className="flex min-h-0 flex-1 flex-col @max-[600px]/shell:hidden">
            <RowActionsContext.Provider value={actions}>
              <DataTable
                title={heading}
                description={countText}
                columns={COLUMNS}
                data={rows}
                isLoading={isLoading}
                fill
                className="flex-1"
                tableHeight={TABLE_HEIGHT}
                getRowId={(row) => row.id}
                getSubRows={treeMode ? (row) => row.children : undefined}
                features={canManage ? SELECTABLE_FEATURES : FEATURES}
                tableOptions={canManage ? tableOptions : undefined}
                initialState={INITIAL_STATE}
                // A saved layout replaces initialState; a new key starts everyone
                // on these widths and hidden columns.
                stateKey="knowledge-documents-2"
                exportFileName="documents"
                rowClassName={rowClass}
                emptyState={none ? empty : undefined}
                statusBarActions={
                  hasMore ? (
                    <Button
                      variant="outline"
                      size="xs"
                      disabled={loadingMore}
                      onClick={onLoadMore}
                    >
                      {loadingMore && <Spinner data-icon="inline-start" />}
                      {loadingMore ? "Loading…" : "Load more"}
                    </Button>
                  ) : undefined
                }
                onRowDoubleClick={(row) => {
                  if (row.original.folder)
                    onOpenCollection(row.original.folder.id)
                  else if (row.original.document) onOpen(row.original.document)
                }}
              />
            </RowActionsContext.Provider>
          </div>
          <div className="hidden min-h-0 flex-1 flex-col @max-[600px]/shell:flex">
            {cards}
          </div>
        </>
      ) : (
        cards
      )}
      {picked.length > 0 && (
        <SelectionBar
          picked={picked}
          collections={collections}
          tree={tree}
          bulk={bulk}
          onClear={() => onSelectedChange({})}
          // Over the rows, clear of the table's status bar.
          className={layout === "list" ? "bottom-14" : "bottom-4"}
        />
      )}
    </div>
  )
}

/* -------------------------------------------------------------------------- */
/* The table                                                                  */
/* -------------------------------------------------------------------------- */

/** A row of the table: a collection, a document, or an upload on its way. */
type DocumentRow = {
  id: string
  filename: string
  /** Where it's filed: its collection's path; empty when unfiled. */
  collection: string
  state: string
  /** Its chunks, once it's searchable. */
  chunks: number | undefined
  size_bytes: number
  uploaded_by: string
  /** When it was uploaded (or made); empty for one still uploading. */
  uploaded_at: string
  document?: KnowledgeDocument
  upload?: Upload
  folder?: DocumentCollection
  /** A folder's collections and documents. */
  children?: DocumentRow[]
}

type Namer = { nameOf: (user: string) => string; me: string | undefined }

function uploadRow(upload: Upload, tree: CollectionTree): DocumentRow {
  return {
    id: `upload:${upload.id}`,
    filename: upload.filename,
    collection: upload.collection_id ? tree.pathName(upload.collection_id) : "",
    state: upload.error ? "Not uploaded" : "Uploading",
    chunks: undefined,
    size_bytes: upload.size_bytes,
    uploaded_by: "You",
    uploaded_at: "",
    upload,
  }
}

function documentRow(
  document: KnowledgeDocument,
  tree: CollectionTree,
  { nameOf, me }: Namer
): DocumentRow {
  return {
    id: document.id,
    filename: document.filename,
    collection: document.collection_id
      ? tree.pathName(document.collection_id)
      : "",
    state: INDEX_STATES[stateOf(document.phase)].label,
    chunks: document.phase === "SUCCEEDED" ? document.chunk_count : undefined,
    size_bytes: document.size_bytes,
    uploaded_by:
      document.created_by === me ? "You" : nameOf(document.created_by),
    uploaded_at: document.created_at,
    document,
  }
}

/** What matches, flat: uploads first, then the documents. */
function toRows(
  uploads: Upload[],
  documents: KnowledgeDocument[],
  tree: CollectionTree,
  nameOf: (user: string) => string,
  me: string | undefined
): DocumentRow[] {
  return [
    ...uploads.map((upload) => uploadRow(upload, tree)),
    ...documents.map((document) => documentRow(document, tree, { nameOf, me })),
  ]
}

/**
 * Where you are, as a tree: its collections as folder rows, each holding its
 * collections and then its documents, and then the documents here. A
 * document (or upload) filed in a collection the tree doesn't know sits
 * at the top.
 */
function toTree(
  uploads: Upload[],
  documents: KnowledgeDocument[],
  tree: CollectionTree,
  location: string | null,
  nameOf: (user: string) => string,
  me: string | undefined
): DocumentRow[] {
  const known = (id: string | null) => (id && tree.byId.has(id) ? id : null)
  const group = <T,>(items: T[], at: (item: T) => string | null) => {
    const by = new Map<string | null, T[]>()
    for (const item of items) {
      const key = known(at(item))
      by.set(key, [...(by.get(key) ?? []), item])
    }
    return by
  }
  const uploadsIn = group(uploads, (upload) => upload.collection_id)
  const documentsIn = group(documents, (document) => document.collection_id)
  const namer = { nameOf, me }
  const contents = (at: string | null): DocumentRow[] => [
    ...(uploadsIn.get(at) ?? []).map((upload) => uploadRow(upload, tree)),
    ...tree.childrenOf(at).map(folderRow),
    ...(documentsIn.get(at) ?? []).map((document) =>
      documentRow(document, tree, namer)
    ),
  ]
  function folderRow(folder: DocumentCollection): DocumentRow {
    const totals = tree.totals(folder.id)
    return {
      id: `folder:${folder.id}`,
      filename: folder.name,
      collection: folder.parent_id ? tree.pathName(folder.parent_id) : "",
      state: `${formatCount(totals.documents)} ${totals.documents === 1 ? "document" : "documents"}`,
      chunks: undefined,
      size_bytes: totals.size_bytes,
      uploaded_by: folder.created_by === me ? "You" : nameOf(folder.created_by),
      uploaded_at: folder.created_at,
      folder,
      children: contents(folder.id),
    }
  }
  // At the top, uploads and documents filed nowhere this tree knows are here.
  if (location === null) return contents(null)
  return [
    ...(uploadsIn.get(null) ?? [])
      .filter((upload) => upload.collection_id === location)
      .map((upload) => uploadRow(upload, tree)),
    ...contents(location),
  ]
}

// The page searches every document on the server (the top bar's search)
// and loads more a page at a time, so the table neither filters nor pages
// what it has; it sorts, sizes, hides columns and exports, and opens and
// closes folders.
const FEATURES: Partial<DataTableFeatureConfig> = {
  ...ADMIN_TABLE_FEATURES,
  expanding: true,
  globalFilter: false,
  pagination: false,
  faceting: false,
}

// Where the page fits the screen (the rail beside it), the table fills what
// the page leaves it. Where the page scrolls instead (the rail below it), the
// table stops at the screen's height: the top bar, the table's toolbar and
// status bar, the page's padding and its footer take the rest, and it never
// shrinks below 18rem. Either way its rows scroll under sticky headers.
const TABLE_HEIGHT =
  "@max-[1270px]/shell:max-h-[max(18rem,calc(100dvh_-_15rem))]"

// How close to the end of the rows (or cards) the next page starts to load.
const LOAD_AHEAD_PX = 240
const SCROLL_REGIONS = new Set(["table-container", "document-cards"])

// A manager ticks documents to act on them all.
const SELECTABLE_FEATURES: Partial<DataTableFeatureConfig> = {
  ...FEATURES,
  rowSelection: true,
}

// Folders start open, like the design system's tree. The row menu stays in
// view when the table scrolls sideways. Who uploaded each document is in the
// Columns menu: the rail names the contributors, and the table fits beside
// it without it.
const INITIAL_STATE: InitialTableState = {
  expanded: true,
  columnPinning: PIN_ACTIONS,
  columnVisibility: { uploaded_by: false },
}

const rowClass = (row: { original: DocumentRow }) =>
  row.original.upload?.error
    ? "bg-danger-surface/60"
    : row.original.upload
      ? "bg-muted/30"
      : ""

type RowActions = Actions &
  CollectionActions & {
    scope: KnowledgeScope
    onDismissUpload: (id: string) => void
  }

// The row menu's actions reach the cells through context, so the columns
// stay the same from render to render.
const RowActionsContext = React.createContext<RowActions | undefined>(undefined)

const helper = createColumnHelper<DocumentRow>()
const COLUMNS = helper.columns([
  helper.accessor("filename", {
    header: "Name",
    // The column that stretches (and carries the tree); this is its narrowest.
    size: 200,
    enableHiding: false,
    meta: { label: "Name" },
    cell: ({ row: { original: row } }) =>
      row.folder ? (
        <FolderName folder={row.folder} />
      ) : (
        <span className="flex min-w-0 items-center gap-3">
          <KindGlyph filename={row.filename} />
          <span className="flex min-w-0 flex-col items-start">
            {row.document ? (
              <DocumentName document={row.document} />
            ) : (
              <span
                className="max-w-full truncate text-[0.8125rem] text-foreground"
                title={row.filename}
              >
                {row.filename}
              </span>
            )}
            {row.document ? (
              <Aside document={row.document} collection={undefined} />
            ) : row.upload?.error ? (
              <span
                className="truncate text-2xs text-danger-foreground"
                title={row.upload.error}
              >
                {row.upload.error}
              </span>
            ) : null}
          </span>
        </span>
      ),
  }),
  helper.accessor("collection", {
    header: "Collection",
    size: 108,
    meta: { label: "Collection", cellClassName: "text-muted-foreground" },
    cell: ({ getValue }) =>
      getValue() || <span className="text-subtle">Unfiled</span>,
  }),
  helper.accessor("state", {
    header: "State",
    size: 108,
    meta: { label: "State" },
    cell: ({ row: { original: row } }) =>
      row.upload ? (
        <UploadState upload={row.upload} />
      ) : row.document ? (
        <StatusBadge status={INDEX_STATES[stateOf(row.document.phase)].status}>
          {row.state}
        </StatusBadge>
      ) : (
        <span className="text-muted-foreground">{row.state}</span>
      ),
  }),
  helper.accessor("chunks", {
    header: "Chunks",
    size: 96,
    sortUndefined: "last",
    meta: {
      label: "Chunks",
      align: "right",
      cellClassName: "text-muted-foreground",
    },
    cell: ({ getValue }) => {
      const chunks = getValue()
      return chunks === undefined ? "–" : formatCount(chunks)
    },
  }),
  helper.accessor("size_bytes", {
    header: "Size",
    size: 88,
    meta: {
      label: "Size",
      align: "right",
      cellClassName: "text-muted-foreground",
    },
    cell: ({ getValue }) => formatBytes(getValue()),
  }),
  helper.accessor("uploaded_by", {
    header: "Uploaded by",
    size: 140,
    meta: { label: "Uploaded by", cellClassName: "text-muted-foreground" },
  }),
  helper.accessor("uploaded_at", {
    header: "Uploaded",
    size: 124,
    meta: { label: "Uploaded", cellClassName: "text-muted-foreground" },
    cell: ({ getValue }) => {
      const at = getValue()
      return at ? <span title={formatDate(at)}>{formatWhen(at)}</span> : "Now"
    },
  }),
  helper.display({
    id: "actions",
    header: () => <span className="sr-only">Actions</span>,
    size: 64,
    enableSorting: false,
    enableHiding: false,
    enableResizing: false,
    meta: { label: "Actions", align: "right" },
    cell: ({ row: { original: row } }) => <RowActionsCell row={row} />,
  }),
])

/** A document row's name, which opens it in the viewer. */
function DocumentName({ document }: { document: KnowledgeDocument }) {
  const actions = React.useContext(RowActionsContext)
  return (
    <button
      type="button"
      title={`Open ${document.filename}`}
      className="max-w-full truncate text-left text-[0.8125rem] text-foreground underline-offset-2 hover:underline"
      // A click on the name opens it; on the row, it doesn't.
      onClick={(event) => {
        event.stopPropagation()
        actions?.onOpen(document)
      }}
    >
      {document.filename}
    </button>
  )
}

/** A folder row's name: its folder and a link that opens the collection. */
function FolderName({ folder }: { folder: DocumentCollection }) {
  const actions = React.useContext(RowActionsContext)
  return (
    <span className="flex min-w-0 items-center gap-3">
      <Icon icon={Folder01Icon} size={16} className="shrink-0 text-subtle" />
      {actions ? (
        <Link
          to="/organizations/$organizationId/knowledge/$knowledgeBaseId"
          params={actions.scope}
          search={(prev) => ({
            ...prev,
            shelf: undefined,
            collection: folder.id,
          })}
          title={folder.description || `Open ${folder.name}`}
          className="truncate text-[0.8125rem] font-medium text-foreground hover:underline"
          // A click on the name opens it; on the row, it doesn't.
          onClick={(event) => event.stopPropagation()}
        >
          {folder.name}
        </Link>
      ) : (
        <span className="truncate text-[0.8125rem] font-medium text-foreground">
          {folder.name}
        </span>
      )}
    </span>
  )
}

function RowActionsCell({ row }: { row: DocumentRow }) {
  const actions = React.useContext(RowActionsContext)
  if (!actions) return null
  if (row.folder) return <FolderMenu folder={row.folder} {...actions} />
  if (row.upload?.error) {
    const { id, filename } = row.upload
    return (
      <Button
        variant="ghost"
        size="icon-xs"
        aria-label={`Dismiss ${filename}`}
        onClick={() => actions.onDismissUpload(id)}
      >
        <Icon icon="close" />
      </Button>
    )
  }
  return row.document ? (
    <DocumentMenu document={row.document} {...actions} />
  ) : null
}

/** A folder row's menu: open it; for a manager, make one in it, rename, delete. */
function FolderMenu({
  folder,
  canManage,
  onOpenCollection,
  onNewCollection,
  onRenameCollection,
  onDeleteCollection,
}: RowActions & { folder: DocumentCollection }) {
  return (
    <DropdownMenu>
      <DropdownMenuTrigger
        render={
          <Button
            variant="ghost"
            size="icon-xs"
            aria-label={`Actions for ${folder.name}`}
          />
        }
      >
        <Icon icon="more" />
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="min-w-48">
        <DropdownMenuItem onClick={() => onOpenCollection(folder.id)}>
          <Icon icon={FolderOpenIcon} />
          Open
        </DropdownMenuItem>
        {canManage && (
          <>
            <DropdownMenuItem onClick={() => onNewCollection(folder)}>
              <Icon icon={FolderAddIcon} />
              New collection in it…
            </DropdownMenuItem>
            <DropdownMenuItem onClick={() => onRenameCollection(folder)}>
              <Icon icon={PencilEdit02Icon} />
              Rename…
            </DropdownMenuItem>
            <DropdownMenuSeparator />
            <DropdownMenuItem
              variant="destructive"
              onClick={() => onDeleteCollection(folder)}
            >
              <Icon icon={Delete02Icon} />
              Delete…
            </DropdownMenuItem>
          </>
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

/* -------------------------------------------------------------------------- */
/* Cards                                                                      */
/* -------------------------------------------------------------------------- */

type RowProps = Actions &
  Pick<DocumentsViewProps, "nameOf" | "me"> & {
    document: KnowledgeDocument
    collection: string | undefined
  }

/** A kind's glyph in graphite: colour is kept for state. */
function KindGlyph({ filename }: { filename: string }) {
  const kind = KINDS[kindOf(filename)]
  return (
    <Icon
      icon={kind.icon}
      size={16}
      className="shrink-0 text-muted-foreground"
    />
  )
}

/** The second line under a name: why it failed, or where it's filed. */
function Aside({
  document,
  collection,
}: {
  document: KnowledgeDocument
  collection: string | undefined
}) {
  const state = stateOf(document.phase)
  if (state === "failed") {
    const reason = describeFailure(document)
    return (
      <span
        className="block truncate text-2xs text-danger-foreground"
        // The worker's own words, for whoever fixes the file.
        title={document.error ? `${reason}\n\n${document.error}` : reason}
      >
        {reason}
      </span>
    )
  }
  if (state === "unknown")
    return (
      <span className="block truncate text-2xs text-subtle">
        The worker stopped reporting on it. Retry it to be sure.
      </span>
    )
  if (collection)
    return (
      <span className="flex items-center gap-1 truncate text-2xs text-subtle">
        <Icon icon={Folder01Icon} size={11} />
        {collection}
      </span>
    )
  return null
}

function DocumentCard({
  document,
  collection,
  selected,
  onSelect,
  nameOf,
  me,
  ...actions
}: RowProps & {
  /** Whether it's ticked; undefined when it can't be. */
  selected: boolean | undefined
  onSelect: (checked: boolean) => void
}) {
  const state = INDEX_STATES[stateOf(document.phase)]
  const who = document.created_by === me ? "you" : nameOf(document.created_by)
  return (
    <li
      className={cn(
        "relative flex min-w-0 flex-col gap-4 rounded-(--radius-card) border px-3.5 pt-3.5 pb-3 transition-[background-color] duration-150 hover:bg-muted/40",
        selected && "border-ring/40 bg-muted/40"
      )}
    >
      <div className="flex items-start justify-between gap-2">
        {/* Above the card-wide button, so they stay their own. */}
        <span className="relative z-10 flex items-center gap-2.5">
          {selected !== undefined && (
            <Checkbox
              aria-label={`Select ${document.filename}`}
              checked={selected}
              onCheckedChange={onSelect}
            />
          )}
          <span className="flex size-7 items-center justify-center rounded-(--radius-soft) bg-muted">
            <KindGlyph filename={document.filename} />
          </span>
        </span>
        <span className="relative z-10 flex items-center gap-1">
          <StatusBadge status={state.status}>{state.label}</StatusBadge>
          <DocumentMenu
            document={document}
            {...actions}
            className="-mt-0.5 -mr-1.5"
          />
        </span>
      </div>
      <div className="grid min-w-0 gap-0.5">
        <button
          type="button"
          title={`Open ${document.filename}`}
          onClick={() => actions.onOpen(document)}
          // The whole card opens it.
          className="truncate text-left text-[0.8125rem] font-medium text-foreground after:absolute after:inset-0 after:rounded-(--radius-card)"
        >
          {document.filename}
        </button>
        <span
          className="truncate text-xs text-muted-foreground tabular-nums"
          title={`Uploaded by ${who}, ${formatDate(document.created_at)}`}
        >
          {formatBytes(document.size_bytes)}
          {document.phase === "SUCCEEDED" &&
            ` · ${formatCount(document.chunk_count)} ${document.chunk_count === 1 ? "chunk" : "chunks"}`}
          {` · ${formatWhen(document.created_at)}`}
        </span>
        <Aside document={document} collection={collection} />
      </div>
    </li>
  )
}

function DocumentMenu({
  document,
  canManage,
  collections,
  tree,
  onOpen,
  onDownload,
  onMove,
  onRetry,
  onRemove,
  className,
}: Actions & { document: KnowledgeDocument; className?: string }) {
  return (
    <DropdownMenu>
      <DropdownMenuTrigger
        render={
          <Button
            variant="ghost"
            size="icon-xs"
            aria-label={`Actions for ${document.filename}`}
            className={className}
          />
        }
      >
        <Icon icon="more" />
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="min-w-48">
        <DropdownMenuItem onClick={() => onOpen(document)}>
          <Icon icon="view" />
          Open
        </DropdownMenuItem>
        <DropdownMenuItem onClick={() => onDownload(document)}>
          <Icon icon="download" />
          Download
        </DropdownMenuItem>
        {canManage && (
          <>
            <DropdownMenuSeparator />
            {canRetry(document.phase) && (
              <>
                <DropdownMenuItem onClick={() => onRetry(document)}>
                  <Icon icon="refresh" />
                  Retry indexing
                </DropdownMenuItem>
                <DropdownMenuSeparator />
              </>
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
 * What to do with the ticked documents, floating at the foot of the list
 * while any are: retry the indexing of those that failed, move them all to
 * a collection, or remove them all. It stays out of the table's toolbar, so
 * ticking a row never moves the rows under the pointer.
 */
function SelectionBar({
  picked,
  collections,
  tree,
  bulk,
  onClear,
  className,
}: {
  picked: KnowledgeDocument[]
  collections: DocumentCollection[] | undefined
  tree: CollectionTree
  bulk: BulkActions
  onClear: () => void
  /** Where it floats over the list, e.g. `bottom-14`. */
  className?: string
}) {
  const retryable = picked.filter((document) => canRetry(document.phase))
  return (
    <div
      role="group"
      aria-label="Selected documents"
      className={cn(
        // Over the foot of the list where the page fits the screen; where the
        // page scrolls, at the foot of the screen instead.
        "absolute inset-x-0 z-30 mx-auto flex w-fit max-w-full flex-wrap items-center gap-1.5 rounded-(--radius-card) border bg-background py-1.5 pr-1.5 pl-3.5 shadow-(--shadow-float) @max-[1270px]/shell:sticky @max-[1270px]/shell:bottom-4 @max-[1270px]/shell:mt-4",
        className
      )}
    >
      <span
        aria-live="polite"
        className="mr-1.5 flex items-center gap-2 text-xs font-medium text-foreground tabular-nums"
      >
        {bulk.busy && <Spinner className="size-3.5 text-muted-foreground" />}
        {formatCount(picked.length)} selected
      </span>
      {retryable.length > 0 && (
        <Button
          variant="outline"
          size="sm"
          disabled={bulk.busy}
          onClick={() => bulk.onRetry(retryable)}
        >
          <Icon icon="refresh" data-icon="inline-start" />
          {retryable.length === picked.length
            ? "Retry indexing"
            : `Retry ${formatCount(retryable.length)}`}
        </Button>
      )}
      <DropdownMenu>
        <DropdownMenuTrigger
          disabled={bulk.busy}
          render={<Button variant="outline" size="sm" />}
        >
          <Icon icon={FolderExportIcon} data-icon="inline-start" />
          Move to
        </DropdownMenuTrigger>
        <DropdownMenuContent side="top" align="start" className="min-w-44">
          <DropdownMenuItem onClick={() => bulk.onMove(picked, null)}>
            Unfiled
          </DropdownMenuItem>
          {collectionChoices(collections, tree).map(({ collection, path }) => (
            <DropdownMenuItem
              key={collection.id}
              onClick={() => bulk.onMove(picked, collection.id)}
            >
              <span className="truncate" title={path}>
                {path}
              </span>
            </DropdownMenuItem>
          ))}
        </DropdownMenuContent>
      </DropdownMenu>
      <Button
        variant="destructive"
        size="sm"
        disabled={bulk.busy}
        onClick={() => bulk.onRemove(picked)}
      >
        <Icon icon={Delete02Icon} data-icon="inline-start" />
        Remove…
      </Button>
      <Button
        variant="ghost"
        size="icon-sm"
        aria-label="Clear selection"
        onClick={onClear}
      >
        <Icon icon="close" />
      </Button>
    </div>
  )
}

/** How far an upload is, or why it stopped. */
function UploadState({ upload }: { upload: Upload }) {
  if (upload.error)
    return (
      <span className="text-2xs font-medium text-danger-foreground">
        Not uploaded
      </span>
    )
  const percent = Math.round(upload.progress * 100)
  return (
    <span className="flex items-center gap-2">
      <span
        role="progressbar"
        aria-label={`Uploading ${upload.filename}`}
        aria-valuenow={percent}
        aria-valuemin={0}
        aria-valuemax={100}
        className="relative h-1 w-12 shrink-0 overflow-hidden rounded-full bg-muted"
      >
        <span
          className="absolute inset-y-0 left-0 rounded-full bg-foreground transition-[width] duration-150 motion-reduce:transition-none"
          style={{ width: `${Math.max(percent, 4)}%` }}
        />
      </span>
      <span className="text-2xs text-muted-foreground tabular-nums">
        {percent === 0 ? "Waiting" : percent < 100 ? `${percent}%` : "Saving"}
      </span>
    </span>
  )
}

function UploadCard({
  upload,
  onDismiss,
}: {
  upload: Upload
  onDismiss: (id: string) => void
}) {
  return (
    <li
      className={cn(
        "flex min-w-0 flex-col gap-4 rounded-(--radius-card) border px-3.5 pt-3.5 pb-3",
        upload.error
          ? "border-danger-border bg-danger-surface/60"
          : "bg-muted/30"
      )}
    >
      <div className="flex items-start justify-between gap-2">
        <span className="flex size-7 items-center justify-center rounded-(--radius-soft) bg-background">
          <KindGlyph filename={upload.filename} />
        </span>
        <span className="flex items-center gap-1">
          <UploadState upload={upload} />
          {upload.error && (
            <Button
              variant="ghost"
              size="icon-xs"
              aria-label={`Dismiss ${upload.filename}`}
              className="-mt-0.5 -mr-1.5"
              onClick={() => onDismiss(upload.id)}
            >
              <Icon icon="close" />
            </Button>
          )}
        </span>
      </div>
      <div className="grid min-w-0 gap-0.5">
        <span className="truncate text-[0.8125rem] font-medium text-foreground">
          {upload.filename}
        </span>
        <span
          className={cn(
            "truncate text-xs tabular-nums",
            upload.error ? "text-danger-foreground" : "text-muted-foreground"
          )}
          title={upload.error}
        >
          {upload.error ?? formatBytes(upload.size_bytes)}
        </span>
      </div>
    </li>
  )
}

/** Cards shaped like the documents, while they load. */
function DocumentsSkeleton() {
  return (
    <div
      className="grid grid-cols-[repeat(auto-fill,minmax(200px,1fr))] gap-3"
      aria-busy="true"
    >
      {Array.from({ length: 8 }, (_, index) => (
        <Skeleton key={index} className="h-[104px] rounded-(--radius-card)" />
      ))}
    </div>
  )
}
