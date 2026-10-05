import * as React from "react"
import {
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query"

import { toApiError, type ApiError } from "@/lib/api/index"
import { createNestedResource } from "@/lib/api/resource"
import { api } from "@/lib/api-instance"
import type { Audited } from "@/lib/hierarchy"
import {
  isUnfinished,
  KINDS,
  SUPPORTED_EXTENSIONS,
  type KindKey,
} from "./knowledge"

/*
 * An organization's knowledge bases, kept by the admin API: each holds its
 * own documents (`…/knowledge-bases/:kb/documents`) and nested collections
 * (`…/knowledge-bases/:kb/document-collections`). Each upload is stored,
 * then the embedding worker parses, chunks and embeds it; its `phase` is
 * that job as last read. Reading takes `organizations:read`; everything else
 * `knowledge_bases:manage`.
 */

/* -------------------------------------------------------------------------- */
/* Knowledge bases                                                            */
/* -------------------------------------------------------------------------- */

export type KnowledgeBase = {
  id: string
  organization_id: string
  name: string
  description: string
  /** Its documents, how many are searchable and how many failed. */
  documents: number
  ready: number
  failed: number
  /** The chunks embedded from its documents. */
  chunks: number
  size_bytes: number
  created_at: string
  created_by: string
  updated_at: string
  updated_by: string
}

export type KnowledgeBaseCreate = { name: string; description?: string }
export type KnowledgeBaseUpdate = Partial<KnowledgeBaseCreate>

export const organizationKnowledgeBases = createNestedResource<
  KnowledgeBase,
  { organizationId: string },
  { create: KnowledgeBaseCreate; update: KnowledgeBaseUpdate }
>({
  api,
  key: "organization-knowledge-bases",
  path: ({ organizationId }) =>
    `/organizations/${encodeURIComponent(organizationId)}/knowledge-bases`,
  label: "knowledge base",
  updateMethod: "patch",
})

/**
 * The organization's knowledge bases. Waits while `organizationId` is
 * undefined.
 */
export function useOrganizationKnowledgeBases(
  organizationId: string | undefined,
  { enabled = true }: { enabled?: boolean } = {}
) {
  return organizationKnowledgeBases
    .scope({ organizationId: organizationId ?? "" })
    .useList(undefined, { enabled: enabled && Boolean(organizationId) })
}

/** One knowledge base. */
export function useKnowledgeBase(scope: KnowledgeScope) {
  return organizationKnowledgeBases
    .scope({ organizationId: scope.organizationId })
    .useDetail(scope.knowledgeBaseId)
}

/** Where a knowledge base's documents and collections live. */
export type KnowledgeScope = { organizationId: string; knowledgeBaseId: string }

const basePath = ({ organizationId, knowledgeBaseId }: KnowledgeScope) =>
  `/organizations/${encodeURIComponent(organizationId)}/knowledge-bases/${encodeURIComponent(knowledgeBaseId)}`

const documentsPath = (scope: KnowledgeScope) => `${basePath(scope)}/documents`
const documentPath = (scope: KnowledgeScope, id: string) =>
  `${documentsPath(scope)}/${encodeURIComponent(id)}`
const collectionsPath = (scope: KnowledgeScope) =>
  `${basePath(scope)}/document-collections`

/* -------------------------------------------------------------------------- */
/* Search                                                                     */
/* -------------------------------------------------------------------------- */

export type SearchHit = {
  chunk_id: string
  document_id: string
  filename: string
  section_path: string[]
  text: string
  score: number
}

export type SearchResult = { hits: SearchHit[] }

/** Searches a knowledge base's chunks as its agents would. */
export function useSearchKnowledgeBase(scope: KnowledgeScope) {
  return useMutation({
    mutationFn: (request: { query: string; limit?: number }) =>
      api.post<SearchResult, { query: string; limit?: number }>(
        `${basePath(scope)}/search`,
        request
      ),
    // The search panel shows why.
    meta: { silent: true },
  })
}

/* -------------------------------------------------------------------------- */
/* Documents                                                                  */
/* -------------------------------------------------------------------------- */

/**
 * The embedding worker's job for a document, as last read: `QUEUED` and
 * `RUNNING` while it ingests; `SUCCEEDED` once its chunks are searchable;
 * `FAILED` with the worker's `error`; `MISSING` when the worker no longer
 * had the job to report on.
 */
export type DocumentPhase =
  "QUEUED" | "RUNNING" | "SUCCEEDED" | "FAILED" | "MISSING"

export type KnowledgeDocument = Audited & {
  id: string
  knowledge_base_id: string
  /** The collection it's filed in; null while unfiled. */
  collection_id: string | null
  filename: string
  media_type: string
  size_bytes: number
  sha256: string
  phase: DocumentPhase
  /** Why it failed, e.g. that no parser reads its format. */
  error: string
  /** The chunks it was split into, once it succeeded. */
  chunk_count: number
  finished_at: string | null
}

/** A named group of the knowledge base's documents, like a folder; they nest. */
export type DocumentCollection = Audited & {
  id: string
  knowledge_base_id: string
  /** The collection it's in; null at the top of the knowledge base. */
  parent_id: string | null
  name: string
  description: string
  /** What's filed in it now, not counting its collections'. */
  document_count: number
  size_bytes: number
}

export type DocumentTotals = {
  documents: number
  size_bytes: number
  chunks: number
  /** How many are searchable (`SUCCEEDED`), and how many `FAILED`. */
  ready: number
  failed: number
}

/** The knowledge base's documents added up, from `GET …/documents/summary`. */
export type DocumentSummary = {
  totals: DocumentTotals
  by_phase: Partial<Record<DocumentPhase, DocumentTotals>>
  /** By extension, lowercase without the dot; `""` for none. */
  by_extension: Record<string, DocumentTotals>
  /** Who uploaded them, most documents first. */
  contributors: (DocumentTotals & { user: string; last_uploaded_at: string })[]
}

/** Documents per page of the list. */
export const DOCUMENTS_PAGE = 50
/** How often to reread while a document is queued or indexing. */
export const INDEXING_POLL_MS = 3000

/** What the list asks the API for. Arrays repeat the parameter. */
export type DocumentFilters = {
  /** Collection IDs, or UNFILED; several list together. */
  collection?: string[]
  phase?: DocumentPhase[]
  extension?: string[]
  not_extension?: string[]
  uploaded_by?: string
  q?: string
}

type DocumentUpdate = { collection_id: string | null }

/**
 * The knowledge base's documents. Its mutations (move, remove) refresh every
 * list and query under its keys, the summary and the collections' counts
 * included.
 */
export const knowledgeDocuments = createNestedResource<
  KnowledgeDocument,
  KnowledgeScope,
  { update: DocumentUpdate }
>({
  api,
  key: "knowledge-documents",
  path: documentsPath,
  label: "document",
  updateMethod: "patch",
})

// FastAPI reads a list from a repeated key: phase=QUEUED&phase=RUNNING.
const repeated = { indexes: null } as const

/** Filters for a kind: its extensions, or for Other none of the known. */
export function kindFilter(kind: KindKey | undefined): DocumentFilters {
  if (!kind) return {}
  if (kind === "other") return { not_extension: SUPPORTED_EXTENSIONS }
  return { extension: [...KINDS[kind].extensions] }
}

/**
 * The knowledge base's documents, most recent first, a page at a time.
 * Reread every few seconds while any listed one is queued or indexing.
 */
export function useKnowledgeDocuments(
  scope: KnowledgeScope,
  filters: DocumentFilters,
  { enabled = true }: { enabled?: boolean } = {}
) {
  const scoped = knowledgeDocuments.scope(scope)
  return useInfiniteQuery({
    enabled,
    queryKey: [...scoped.keys.infiniteLists(), filters],
    queryFn: ({ pageParam, signal }) =>
      api.get<KnowledgeDocument[]>(documentsPath(scope), {
        params: { ...filters, limit: DOCUMENTS_PAGE, offset: pageParam },
        paramsSerializer: repeated,
        signal,
      }),
    initialPageParam: 0,
    getNextPageParam: (last, pages) =>
      last.length < DOCUMENTS_PAGE ? undefined : pages.length * DOCUMENTS_PAGE,
    refetchInterval: (query) =>
      query.state.data?.pages.some((page) =>
        page.some((document) => isUnfinished(document.phase))
      )
        ? INDEXING_POLL_MS
        : false,
  })
}

/**
 * The knowledge base's documents added up. Under the documents' keys, so
 * uploads, moves and removals refresh it; reread with the list while any is
 * unfinished.
 */
export function useDocumentSummary(scope: KnowledgeScope) {
  const scoped = knowledgeDocuments.scope(scope)
  return useQuery({
    queryKey: [...scoped.keys.all, "summary"],
    queryFn: ({ signal }) =>
      api.get<DocumentSummary>(`${documentsPath(scope)}/summary`, { signal }),
    refetchInterval: (query) => {
      const phases = query.state.data?.by_phase
      return phases?.QUEUED?.documents || phases?.RUNNING?.documents
        ? INDEXING_POLL_MS
        : false
    },
  })
}

/** The latest documents, for the activity feed. */
export function useRecentDocuments(scope: KnowledgeScope, limit = 12) {
  const scoped = knowledgeDocuments.scope(scope)
  return useQuery({
    queryKey: [...scoped.keys.lists(), { recent: limit }],
    queryFn: ({ signal }) =>
      api.get<KnowledgeDocument[]>(documentsPath(scope), {
        params: { limit },
        signal,
      }),
    refetchInterval: (query) =>
      query.state.data?.some((document) => isUnfinished(document.phase))
        ? INDEXING_POLL_MS
        : false,
  })
}

/* -------------------------------------------------------------------------- */
/* Files                                                                      */
/* -------------------------------------------------------------------------- */

// Reading a large file back can take a while on a slow link.
const CONTENT_TIMEOUT_MS = 10 * 60 * 1000

/**
 * A document's file as it was uploaded, from `GET …/documents/:id/content`.
 * Its errors say why in the API's words (the body of a failed Blob request
 * is read for its `detail`).
 */
export async function fetchDocumentContent(
  scope: KnowledgeScope,
  documentId: string,
  {
    signal,
    onProgress,
  }: { signal?: AbortSignal; onProgress?: (share: number) => void } = {}
) {
  try {
    return await api.get<Blob>(`${documentPath(scope, documentId)}/content`, {
      responseType: "blob",
      timeout: CONTENT_TIMEOUT_MS,
      signal,
      onDownloadProgress: (event) => {
        if (event.total) onProgress?.(event.loaded / event.total)
      },
    })
  } catch (thrown) {
    const error = toApiError(thrown)
    if (error.data instanceof Blob && /json/.test(error.data.type)) {
      try {
        const body = JSON.parse(await error.data.text()) as { detail?: unknown }
        if (typeof body.detail === "string") error.message = body.detail
      } catch {
        // Not JSON after all: the generic message stands.
      }
    }
    throw error
  }
}

const contentKey = (scope: KnowledgeScope, document: KnowledgeDocument) =>
  [
    "knowledge-document-content",
    scope.organizationId,
    scope.knowledgeBaseId,
    document.id,
    document.sha256,
  ] as const

/**
 * A document's file for the viewer, with how much of it has arrived. Kept a
 * few minutes after it's closed, so paging back to it is instant; a file
 * never changes, so it's never refetched while kept.
 */
export function useDocumentContent(
  scope: KnowledgeScope,
  document: KnowledgeDocument | undefined
) {
  const [progress, setProgress] = React.useState<{
    id: string
    share: number
  }>()
  const query = useQuery({
    queryKey: document
      ? contentKey(scope, document)
      : [
          "knowledge-document-content",
          scope.organizationId,
          scope.knowledgeBaseId,
          "none",
        ],
    queryFn: ({ signal }) =>
      fetchDocumentContent(scope, document!.id, {
        signal,
        onProgress: (share) => setProgress({ id: document!.id, share }),
      }),
    enabled: document !== undefined,
    staleTime: Infinity,
    gcTime: 5 * 60 * 1000,
    // The viewer shows why.
    meta: { silent: true },
  })
  return {
    ...query,
    progress: progress?.id === document?.id ? progress?.share : undefined,
  }
}

/**
 * Saves a document's file to the person's computer, from the viewer's copy
 * when it has one.
 */
export function useDownloadDocument(scope: KnowledgeScope) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async (document: KnowledgeDocument) => {
      const blob = await queryClient.fetchQuery({
        queryKey: contentKey(scope, document),
        queryFn: ({ signal }) =>
          fetchDocumentContent(scope, document.id, { signal }),
        staleTime: Infinity,
        gcTime: 5 * 60 * 1000,
      })
      const url = URL.createObjectURL(blob)
      const link = window.document.createElement("a")
      link.href = url
      link.download = document.filename
      link.rel = "noopener"
      window.document.body.append(link)
      link.click()
      link.remove()
      // After the browser has taken the file.
      window.setTimeout(() => URL.revokeObjectURL(url), 30_000)
    },
    meta: { errorTitle: "Couldn't download the document" },
  })
}

/* -------------------------------------------------------------------------- */
/* Uploads                                                                    */
/* -------------------------------------------------------------------------- */

/** A file on its way to the admin API. */
export type Upload = {
  id: string
  filename: string
  size_bytes: number
  collection_id: string | null
  /** 0–1 of the bytes sent. */
  progress: number
  error?: string
}

/**
 * Uploads files one after another, keeping each one's progress for the list
 * to show until the API has it. Settled uploads refresh the documents, the
 * collections and the knowledge bases' counts; a failed one stays, with the
 * API's reason, until dismissed.
 */
export function useUploads(scope: KnowledgeScope) {
  const queryClient = useQueryClient()
  const [uploads, setUploads] = React.useState<Upload[]>([])
  const queue = React.useRef(Promise.resolve())
  const { organizationId, knowledgeBaseId } = scope

  const patch = React.useCallback(
    (id: string, change: Partial<Upload>) =>
      setUploads((current) =>
        current.map((upload) =>
          upload.id === id ? { ...upload, ...change } : upload
        )
      ),
    []
  )

  const refresh = React.useCallback(() => {
    const at = { organizationId, knowledgeBaseId }
    return Promise.all([
      queryClient.invalidateQueries({
        queryKey: knowledgeDocuments.scope(at).keys.all,
      }),
      queryClient.invalidateQueries({
        queryKey: documentCollections.scope(at).keys.all,
      }),
      queryClient.invalidateQueries({
        queryKey: organizationKnowledgeBases.scope({ organizationId }).keys.all,
      }),
    ])
  }, [queryClient, organizationId, knowledgeBaseId])

  const send = React.useCallback(
    (upload: Upload, file: File) => {
      const form = new FormData()
      form.append("file", file)
      if (upload.collection_id)
        form.append("collection_id", upload.collection_id)
      return api
        .post<KnowledgeDocument, FormData>(
          documentsPath({ organizationId, knowledgeBaseId }),
          form,
          {
            onUploadProgress: (event) =>
              patch(upload.id, {
                progress: event.total ? event.loaded / event.total : 0,
              }),
          }
        )
        .then(async () => {
          await refresh()
          setUploads((current) => current.filter((u) => u.id !== upload.id))
        })
        .catch((error: unknown) =>
          patch(upload.id, { error: toApiError(error).message, progress: 0 })
        )
    },
    [organizationId, knowledgeBaseId, patch, refresh]
  )

  const start = React.useCallback(
    (placed: { file: File; collectionId: string | null }[]) => {
      const added = placed.map(({ file, collectionId }) => ({
        file,
        upload: {
          id: crypto.randomUUID(),
          filename: file.name,
          size_bytes: file.size,
          collection_id: collectionId,
          progress: 0,
        } satisfies Upload,
      }))
      setUploads((current) => [
        ...added.map(({ upload }) => upload),
        ...current,
      ])
      for (const { upload, file } of added) {
        queue.current = queue.current.then(() => send(upload, file))
      }
    },
    [send]
  )

  const dismiss = React.useCallback(
    (id: string) =>
      setUploads((current) => current.filter((upload) => upload.id !== id)),
    []
  )

  return { uploads, start, dismiss }
}

/* -------------------------------------------------------------------------- */
/* Collections                                                                */
/* -------------------------------------------------------------------------- */

export type CollectionInput = {
  name: string
  description: string
  /** Where a new one goes; the top of the knowledge base when null or absent. */
  parent_id?: string | null
}

/** The collection at the end of one folder path, as the API made or found it. */
type CollectionAtPath = { path: string[]; collection: DocumentCollection }

export const documentCollections = createNestedResource<
  DocumentCollection,
  KnowledgeScope,
  { create: CollectionInput; update: Partial<CollectionInput> }
>({
  api,
  key: "knowledge-document-collections",
  path: collectionsPath,
  label: "collection",
  updateMethod: "patch",
})

/** Something to do to each of several documents at once. */
export type BulkAction =
  | { action: "retry" }
  | { action: "move"; collectionId: string | null }
  | { action: "remove" }

/** How a bulk action went: how many it did, and those it couldn't, why. */
export type BulkResult = {
  done: number
  failed: { document: KnowledgeDocument; error: ApiError }[]
}

// Documents acted on at once: a long selection neither floods the API nor
// waits on each document in turn.
const BULK_CONCURRENCY = 6

/** Runs `act` on each document, a few at a time, keeping every failure. */
async function eachDocument(
  documents: readonly KnowledgeDocument[],
  act: (document: KnowledgeDocument) => Promise<unknown>
): Promise<BulkResult> {
  const failed: BulkResult["failed"] = []
  let next = 0
  const work = async () => {
    for (let at = next++; at < documents.length; at = next++) {
      const document = documents[at]
      try {
        await act(document)
      } catch (error) {
        failed.push({ document, error: toApiError(error) })
      }
    }
  }
  await Promise.all(
    Array.from({ length: Math.min(BULK_CONCURRENCY, documents.length) }, work)
  )
  return { done: documents.length - failed.length, failed }
}

/**
 * Removing a document or a collection changes the other's counts too, so
 * each also refreshes the other's queries (and the knowledge bases' counts).
 * Retrying a document's indexing queues it again, so its lists poll until
 * it's done. `bulk` retries, moves or removes several documents, each on its
 * own, and never throws: its result says which it couldn't.
 */
export function useKnowledgeMutations(scope: KnowledgeScope) {
  const queryClient = useQueryClient()
  const documents = knowledgeDocuments.scope(scope)
  const collections = documentCollections.scope(scope)
  const bases = organizationKnowledgeBases.scope({
    organizationId: scope.organizationId,
  })
  const refreshAll = () =>
    Promise.all([
      queryClient.invalidateQueries({ queryKey: documents.keys.all }),
      queryClient.invalidateQueries({ queryKey: collections.keys.all }),
      queryClient.invalidateQueries({ queryKey: bases.keys.all }),
    ])

  const move = useMutation({
    mutationFn: ({
      id,
      collectionId,
    }: {
      id: string
      collectionId: string | null
    }) =>
      api.patch<KnowledgeDocument, DocumentUpdate>(documentPath(scope, id), {
        collection_id: collectionId,
      }),
    meta: { errorTitle: "Couldn't move the document" },
    onSuccess: refreshAll,
  })
  const retry = useMutation({
    mutationFn: (id: string) =>
      api.post<KnowledgeDocument>(`${documentPath(scope, id)}/retry`),
    meta: { errorTitle: "Couldn't retry indexing" },
    onSuccess: refreshAll,
  })
  const remove = useMutation({
    mutationFn: (id: string) => api.delete<void>(documentPath(scope, id)),
    // The dialog shows why it failed.
    meta: { silent: true },
    onSuccess: refreshAll,
  })
  const bulk = useMutation({
    mutationFn: ({
      documents: picked,
      ...request
    }: BulkAction & { documents: readonly KnowledgeDocument[] }) =>
      eachDocument(picked, (document) => {
        const one = documentPath(scope, document.id)
        if (request.action === "retry")
          return api.post<KnowledgeDocument>(`${one}/retry`)
        if (request.action === "move")
          return api.patch<KnowledgeDocument, DocumentUpdate>(one, {
            collection_id: request.collectionId,
          })
        return api.delete<void>(one)
      }),
    // The page says what it did and what it couldn't.
    meta: { silent: true },
    onSettled: refreshAll,
  })
  const createCollection = useMutation({
    mutationFn: (input: CollectionInput) =>
      api.post<DocumentCollection, CollectionInput>(
        collectionsPath(scope),
        input
      ),
    meta: { silent: true },
    onSuccess: refreshAll,
  })
  const updateCollection = useMutation({
    mutationFn: ({ id, input }: { id: string; input: CollectionInput }) =>
      api.patch<DocumentCollection, CollectionInput>(
        `${collectionsPath(scope)}/${encodeURIComponent(id)}`,
        input
      ),
    meta: { silent: true },
    onSuccess: refreshAll,
  })
  // An uploaded folder's tree as collections under parent, made or found.
  const ensurePaths = useMutation({
    mutationFn: ({
      parentId,
      paths,
    }: {
      parentId: string | null
      paths: string[][]
    }) =>
      api.post<{ collections: CollectionAtPath[] }>(
        `${collectionsPath(scope)}/paths`,
        { parent_id: parentId, paths }
      ),
    meta: { errorTitle: "Couldn't create the folder's collections" },
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: collections.keys.all }),
  })
  const deleteCollection = useMutation({
    mutationFn: (id: string) =>
      api.delete<void>(`${collectionsPath(scope)}/${encodeURIComponent(id)}`),
    meta: { silent: true },
    onSuccess: refreshAll,
  })
  return {
    move,
    retry,
    remove,
    bulk,
    createCollection,
    updateCollection,
    ensurePaths,
    deleteCollection,
  }
}
