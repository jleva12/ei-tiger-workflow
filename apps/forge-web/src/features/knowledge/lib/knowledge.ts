import {
  BookOpen01Icon,
  Doc01Icon,
  FileEmpty01Icon,
  Flowchart01Icon,
  Pdf01Icon,
  Ppt01Icon,
  Txt01Icon,
  Xls01Icon,
} from "@hugeicons/core-free-icons"

import type { IconProp } from "@/components/forge/icons"
import type { TaskStatus } from "@/components/forge/variants"
import { isTimestamp, parseTimestamp } from "@/lib/timestamps"
import type {
  DocumentCollection,
  DocumentPhase,
  DocumentSummary,
  DocumentTotals,
  KnowledgeDocument,
} from "./api"

/*
 * What the Knowledge page knows without asking the API: the kinds of file
 * the embedding worker reads, a document's index state, how the page words
 * sizes and times, how collections nest, and what an upload of files or
 * folders holds.
 */

const noTotals = (): DocumentTotals => ({
  documents: 0,
  size_bytes: 0,
  chunks: 0,
  ready: 0,
  failed: 0,
})

function addTotals(into: DocumentTotals, sum: DocumentTotals) {
  into.documents += sum.documents
  into.size_bytes += sum.size_bytes
  into.chunks += sum.chunks
  into.ready += sum.ready ?? 0
  into.failed += sum.failed ?? 0
}

/* -------------------------------------------------------------------------- */
/* Kinds and states                                                           */
/* -------------------------------------------------------------------------- */

export type KindKey =
  "pdf" | "word" | "slides" | "sheets" | "text" | "diagrams" | "other"

type Kind = {
  label: string
  icon: IconProp
  /** The extensions the embedding worker parses as this kind. */
  extensions: readonly string[]
}

/**
 * The kinds of file the embedding worker reads, by the extensions its
 * parsers take; anything else is Other, which it can't index.
 */
export const KINDS: Record<KindKey, Kind> = {
  pdf: { label: "PDF", icon: Pdf01Icon, extensions: ["pdf"] },
  // Legacy (.doc, .ppt, .xls), RTF and OpenDocument files are converted with
  // LibreOffice on the worker before they're read.
  word: {
    label: "Word",
    icon: Doc01Icon,
    extensions: ["docx", "docm", "doc", "dot", "rtf", "odt"],
  },
  slides: {
    label: "PowerPoint",
    icon: Ppt01Icon,
    extensions: ["pptx", "pptm", "ppsx", "ppsm", "ppt", "pps", "pot", "odp"],
  },
  sheets: {
    label: "Spreadsheets",
    icon: Xls01Icon,
    extensions: ["xlsx", "xlsm", "xls", "xlt", "ods", "csv", "tsv"],
  },
  text: {
    label: "Markdown & text",
    icon: Txt01Icon,
    extensions: ["md", "markdown", "mdx", "txt", "text", "log"],
  },
  // Visio drawings, and Mermaid diagrams (e.g. those the assistant writes).
  diagrams: {
    label: "Diagrams",
    icon: Flowchart01Icon,
    extensions: ["vsdx", "vsdm", "mmd", "mermaid"],
  },
  other: { label: "Other", icon: FileEmpty01Icon, extensions: [] },
}

export const KIND_KEYS = Object.keys(KINDS) as KindKey[]

export const isKindKey = (value: unknown): value is KindKey =>
  typeof value === "string" && Object.hasOwn(KINDS, value)

/** Every extension the worker parses, for the file picker and checks. */
export const SUPPORTED_EXTENSIONS = KIND_KEYS.flatMap(
  (key) => KINDS[key].extensions
)

export const extensionOf = (filename: string) => {
  const dot = filename.lastIndexOf(".")
  return dot > 0 ? filename.slice(dot + 1).toLowerCase() : ""
}

export function kindOf(filename: string): KindKey {
  const extension = extensionOf(filename)
  return (
    KIND_KEYS.find((key) => KINDS[key].extensions.includes(extension)) ??
    "other"
  )
}

/** Totals by kind, from the summary's totals by extension. */
export function totalsByKind(summary: DocumentSummary | undefined) {
  const totals = Object.fromEntries(
    KIND_KEYS.map((key) => [key, noTotals()])
  ) as Record<KindKey, DocumentTotals>
  for (const [extension, sum] of Object.entries(summary?.by_extension ?? {})) {
    const kind =
      KIND_KEYS.find((key) => KINDS[key].extensions.includes(extension)) ??
      "other"
    addTotals(totals[kind], sum)
  }
  return totals
}

/** Where a document stands for agents, in the page's words. */
export type IndexState = "ready" | "indexing" | "queued" | "failed" | "unknown"

export const INDEX_STATES: Record<
  IndexState,
  {
    label: string
    status: TaskStatus
    phases: readonly DocumentPhase[]
    /** The bar's fill: the state's own status ink. */
    bar: string
  }
> = {
  ready: {
    label: "Ready",
    status: "completed",
    phases: ["SUCCEEDED"],
    bar: "bg-status-success-foreground",
  },
  indexing: {
    label: "Indexing",
    status: "running",
    phases: ["RUNNING"],
    bar: "bg-status-running-foreground",
  },
  queued: {
    label: "Queued",
    status: "enqueued",
    phases: ["QUEUED"],
    bar: "bg-status-queued-foreground",
  },
  failed: {
    label: "Failed",
    status: "failed",
    phases: ["FAILED"],
    bar: "bg-status-failed-foreground",
  },
  unknown: {
    label: "Unconfirmed",
    status: "pending",
    phases: ["MISSING"],
    bar: "bg-subtle",
  },
}

export const INDEX_STATE_KEYS = Object.keys(INDEX_STATES) as IndexState[]

export const stateOf = (phase: DocumentPhase): IndexState =>
  INDEX_STATE_KEYS.find((key) => INDEX_STATES[key].phases.includes(phase)) ??
  "unknown"

export const isUnfinished = (phase: DocumentPhase) =>
  phase === "QUEUED" || phase === "RUNNING"

/** Its indexing can be retried: it failed, or the worker lost track of it. */
export const canRetry = (phase: DocumentPhase) =>
  phase === "FAILED" || phase === "MISSING"

/** Totals by index state, from the summary's totals by phase. */
export function totalsByState(summary: DocumentSummary | undefined) {
  const totals = Object.fromEntries(
    INDEX_STATE_KEYS.map((key) => [key, noTotals()])
  ) as Record<IndexState, DocumentTotals>
  for (const [phase, sum] of Object.entries(summary?.by_phase ?? {})) {
    addTotals(totals[stateOf(phase as DocumentPhase)], sum)
  }
  return totals
}

/** The share of the documents ready to search, as a whole percent. */
export function readyShare(totals: Record<IndexState, DocumentTotals>) {
  const total = INDEX_STATE_KEYS.reduce(
    (sum, key) => sum + totals[key].documents,
    0
  )
  return total ? Math.round((totals.ready.documents / total) * 100) : 0
}

export const KNOWLEDGE_ICON: IconProp = BookOpen01Icon

const SAVE_AS: Record<string, string> = {
  vsd: ".vsdx",
}

/** What the worker converts a legacy or OpenDocument file to. */
const CONVERTS_TO: Record<string, string> = {
  ppt: ".pptx",
  pps: ".pptx",
  pot: ".pptx",
  odp: ".pptx",
  doc: ".docx",
  dot: ".docx",
  rtf: ".docx",
  odt: ".docx",
  xls: ".xlsx",
  xlt: ".xlsx",
  ods: ".xlsx",
}

/**
 * Why a document failed, in Forge's words and with the way out; the
 * worker's own message stays available as the detail.
 */
export function describeFailure(
  document: Pick<KnowledgeDocument, "filename" | "error">
) {
  const extension = extensionOf(document.filename)
  const error = document.error
  if (
    /UnsupportedFormat|no parser/i.test(error) ||
    kindOf(document.filename) === "other"
  ) {
    const target = SAVE_AS[extension]
    return target
      ? `Forge can't read .${extension} files. Save it as ${target} and upload it again.`
      : `Forge can't read ${extension ? `.${extension}` : "this kind of"} files. Upload PDF, Word, PowerPoint, Excel, OpenDocument, CSV, Markdown, text, Visio or Mermaid instead.`
  }
  if (/LibreOffice/.test(error)) {
    const target = CONVERTS_TO[extension] ?? "a current Office format"
    return /not installed/i.test(error)
      ? `Forge can't convert .${extension} files here yet. Save it as ${target} and upload it again.`
      : `Forge couldn't convert it. Open it, save it as ${target} and upload it again.`
  }
  if (/password-protected|encrypt/i.test(error))
    return "It's password-protected. Remove the password and upload it again."
  if (/over the OCR limit/i.test(error))
    return "It has more scanned pages than Forge reads by OCR. Split it into smaller files and upload them."
  if (/no text in pdf/i.test(error))
    return "Forge couldn't find any text in it, even by reading the page images. If it's a scan, make sure it's legible and upload it again."
  if (/BadZipFile|not a zip file|corrupt|damaged/i.test(error))
    return `It isn't a readable ${KINDS[kindOf(document.filename)].label} file. Re-save it and upload it again.`
  if (/timed? ?out|timeout/i.test(error))
    return "Indexing ran out of time. Retry it, or remove it and upload it again."
  if (/empty|no text|no content/i.test(error))
    return "There's no text in it to index."
  return "The worker couldn't index it. Retry it, or remove it and upload it again."
}

/* -------------------------------------------------------------------------- */
/* Where the page looks                                                       */
/* -------------------------------------------------------------------------- */

/** The sidebar's views besides All documents and the collections. */
export type KnowledgeShelf = "indexing" | "failed" | "mine"

export const isKnowledgeShelf = (value: unknown): value is KnowledgeShelf =>
  value === "indexing" || value === "failed" || value === "mine"

/**
 * What the sidebar picked: a shelf, or a collection (or `UNFILED`), or
 * neither for all the documents. Never both.
 */
export type KnowledgePlace = { shelf?: KnowledgeShelf; collection?: string }

/** The page's search params: where it looks, its filters and layout. */
export type KnowledgeSearch = KnowledgePlace & {
  kind?: KindKey
  q?: string
  /** Cards instead of rows. */
  layout?: "grid"
  /** The document open in the viewer, over the page. */
  document?: string
}

/* -------------------------------------------------------------------------- */
/* Formatting                                                                 */
/* -------------------------------------------------------------------------- */

const UNITS = ["B", "KB", "MB", "GB", "TB"]

/** "12.4 MB": decimal units, one decimal under 100. */
export function formatBytes(bytes: number) {
  let value = bytes
  let unit = 0
  while (value >= 1000 && unit < UNITS.length - 1) {
    value /= 1000
    unit += 1
  }
  const digits = unit === 0 || value >= 100 ? 0 : 1
  return `${value.toFixed(digits).replace(/\.0$/, "")} ${UNITS[unit]}`
}

const relative = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" })
const dateFormat = new Intl.DateTimeFormat(undefined, {
  month: "short",
  day: "numeric",
  year: "numeric",
})

/** "3 hours ago", "yesterday"; past a week, the date. */
export function formatWhen(iso: string, now = Date.now()) {
  const at = parseTimestamp(iso)
  if (Number.isNaN(at.getTime())) return iso
  const seconds = Math.round((at.getTime() - now) / 1000)
  const abs = Math.abs(seconds)
  if (abs < 45) return "just now"
  if (abs < 3600) return relative.format(Math.round(seconds / 60), "minute")
  if (abs < 86400) return relative.format(Math.round(seconds / 3600), "hour")
  if (abs < 7 * 86400)
    return relative.format(Math.round(seconds / 86400), "day")
  return dateFormat.format(at)
}

export const formatDate = (iso: string) =>
  isTimestamp(iso) ? dateFormat.format(parseTimestamp(iso)) : iso

export const formatCount = (value: number) => value.toLocaleString()

/** `collection` for the documents in no collection. */
export const UNFILED = "none"

/* -------------------------------------------------------------------------- */
/* Collections and uploads                                                    */
/* -------------------------------------------------------------------------- */

/** How the collections nest, for walking them. */
export type CollectionTree = {
  byId: Map<string, DocumentCollection>
  /** A collection's collections, or the top ones for null, by name. */
  childrenOf: (id: string | null) => DocumentCollection[]
  /** The collections from the top down to this one, itself included. */
  pathOf: (id: string) => DocumentCollection[]
  /** "Runbooks / payments". */
  pathName: (id: string) => string
  /** This collection and every one in it, at any depth. */
  subtreeIds: (id: string) => string[]
  /** Documents and bytes in this collection and those in it. */
  totals: (id: string) => { documents: number; size_bytes: number }
}

const byName = (a: DocumentCollection, b: DocumentCollection) =>
  a.name.localeCompare(b.name, undefined, { sensitivity: "base" })

export function collectionTree(
  collections: DocumentCollection[] | undefined
): CollectionTree {
  const list = collections ?? []
  const byId = new Map(list.map((c) => [c.id, c]))
  const children = new Map<string | null, DocumentCollection[]>()
  for (const c of list) {
    // One whose parent is gone sits at the top.
    const parent = c.parent_id && byId.has(c.parent_id) ? c.parent_id : null
    children.set(parent, [...(children.get(parent) ?? []), c])
  }
  for (const siblings of children.values()) siblings.sort(byName)
  const childrenOf = (id: string | null) => children.get(id) ?? []
  const pathOf = (id: string) => {
    const path: DocumentCollection[] = []
    const seen = new Set<string>()
    for (let c = byId.get(id); c && !seen.has(c.id);) {
      seen.add(c.id)
      path.unshift(c)
      c = c.parent_id ? byId.get(c.parent_id) : undefined
    }
    return path
  }
  const subtreeIds = (id: string) => {
    const ids: string[] = []
    const walk = (at: string) => {
      if (ids.includes(at)) return
      ids.push(at)
      for (const child of childrenOf(at)) walk(child.id)
    }
    walk(id)
    return ids
  }
  return {
    byId,
    childrenOf,
    pathOf,
    pathName: (id) =>
      pathOf(id)
        .map((c) => c.name)
        .join(" / "),
    subtreeIds,
    totals: (id) =>
      subtreeIds(id).reduce(
        (sum, at) => {
          const c = byId.get(at)
          return {
            documents: sum.documents + (c?.document_count ?? 0),
            size_bytes: sum.size_bytes + (c?.size_bytes ?? 0),
          }
        },
        { documents: 0, size_bytes: 0 }
      ),
  }
}

/** The knowledge base's collections by their paths, for moving documents into. */
export function collectionChoices(
  collections: DocumentCollection[] | undefined,
  tree: CollectionTree
) {
  return (collections ?? [])
    .map((collection) => ({ collection, path: tree.pathName(collection.id) }))
    .sort((a, b) => a.path.localeCompare(b.path))
}

/**
 * A file to upload and the folders it was in, relative to what was picked
 * or dropped: ["Runbooks", "payments"] for Runbooks/payments/refunds.md, and
 * none for a file picked on its own.
 */
export type UploadItem = { file: File; folders: string[] }

/** A file picked from a folder picker, with its folders. */
export function itemOfPickedFile(file: File): UploadItem {
  const parts = (file.webkitRelativePath || file.name).split("/")
  return { file, folders: parts.slice(0, -1).filter(Boolean) }
}

/**
 * Splits what was picked or dropped into what agents can read and
 * what they can't. Hidden files and folders (.DS_Store, .git) are left out
 * without a word, as the system's own.
 */
export function sortUploadItems(items: UploadItem[]) {
  const hidden = (name: string) => name.startsWith(".")
  const visible = items.filter(
    ({ file, folders }) => !hidden(file.name) && !folders.some(hidden)
  )
  const readable = visible.filter(({ file }) =>
    SUPPORTED_EXTENSIONS.includes(extensionOf(file.name))
  )
  return {
    readable,
    skipped: visible.filter((item) => !readable.includes(item)),
  }
}

/**
 * Walks a dropped folder: every file in it, at any depth, with the folders
 * it was in (the dropped folder first).
 */
export async function readDroppedEntries(
  entries: FileSystemEntry[]
): Promise<UploadItem[]> {
  const items: UploadItem[] = []
  const walk = async (entry: FileSystemEntry, folders: string[]) => {
    if (entry.isFile) {
      const file = await new Promise<File>((resolve, reject) =>
        (entry as FileSystemFileEntry).file(resolve, reject)
      )
      items.push({ file, folders })
      return
    }
    if (!entry.isDirectory) return
    const reader = (entry as FileSystemDirectoryEntry).createReader()
    const inside = [...folders, entry.name]
    // readEntries answers in batches until an empty one.
    for (;;) {
      const batch = await new Promise<FileSystemEntry[]>((resolve, reject) =>
        reader.readEntries(resolve, reject)
      )
      if (batch.length === 0) break
      for (const child of batch) await walk(child, inside)
    }
  }
  for (const entry of entries) await walk(entry, [])
  return items
}
