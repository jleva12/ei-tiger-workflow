import * as React from "react"

import { toApiError } from "@/lib/api/index"
import { documentOf, useBuilderApi } from "./store"

/*
 * Saving what's built (a workflow, an agent) to the organization as it's
 * built. A change is saved once
 * editing pauses; a step added, removed or connected is saved at once. One
 * save runs at a time, each made from the revision the last one returned,
 * so a teammate's save in between is noticed (409) rather than overwritten.
 * A save that can't reach the API tries again, sooner then later; one the
 * API refuses waits for the next change. Leaving warns while work is
 * unsaved; pagehide makes a best-effort save through the same queue.
 */

// How long editing pauses before a save, in milliseconds.
const PAUSE_MS = 800
const RETRY_MS = { first: 2_000, most: 30_000 }

export type SaveState =
  | { kind: "saved"; at: number }
  | { kind: "saving" }
  /** It couldn't reach the API; it tries again at `next`. */
  | { kind: "retrying"; error: string; next: number }
  /** The API refused it (e.g. no permission to save); the next change tries again. */
  | { kind: "refused"; error: string; status?: number }
  /** Someone saved it since; nothing more is saved until the person chooses. */
  | { kind: "conflict" }

export type Autosave = {
  state: SaveState
  /** Save now, e.g. when retrying by hand. */
  saveNow: () => void
  /** Give up these changes for the version someone else saved. */
  loadTheirs: () => Promise<void>
  /** Save these changes over the version someone else saved. */
  keepMine: () => Promise<void>
  /**
   * Save what's unsaved now and wait for it: the revision everything is
   * saved at, or null when it couldn't be (a conflict, a refusal, an error).
   */
  flush: () => Promise<number | null>
}

// What saving compares: the document without the times the API sets.
const comparable = (doc: unknown) =>
  JSON.stringify({ ...(doc as object), created_at: "", updated_at: "" })

/** A saved document as the API keeps it: its revision and when it was saved. */
export type SavedRecord<Doc> = {
  id: string
  revision: number
  updated_at: string
  document: Doc
}

/** How a builder's documents are saved, read back and cached. */
export type SaveIo<Doc, Rec extends SavedRecord<Doc>> = {
  /** Save the next version, made from `revision`; rejects with an ApiError (409 when saved since). */
  save: (
    id: string,
    body: { document: Doc; revision: number },
    options: { keepalive: boolean }
  ) => Promise<Rec>
  /** The version the organization has now. */
  fetch: (id: string) => Promise<Rec>
  /** Keep a record the API answered with, where the rest of the app reads it. */
  cache: (record: Rec) => void
}

export function useBuilderAutosave<Doc, Rec extends SavedRecord<Doc>>(
  initial: Rec,
  io: SaveIo<Doc, Rec>
): Autosave {
  const api = useBuilderApi()
  // The latest way to save, read when saving.
  const ioRef = React.useRef(io)
  React.useEffect(() => {
    ioRef.current = io
  })
  const [state, setState] = React.useState<SaveState>(() => ({
    kind: "saved",
    at: Date.parse(initial.updated_at),
  }))
  const controls = React.useRef<Omit<Autosave, "state"> | null>(null)
  // Read once: the builder owns the document from here.
  const [start] = React.useState(initial)

  React.useEffect(() => {
    const id = start.id
    let revision = start.revision
    let saved = comparable(documentOf(api.getState()))
    // A version the API refused, not sent again until it changes.
    let refused: string | undefined
    let halted = false
    let attempt = 0
    let disposed = false
    let timer: ReturnType<typeof setTimeout> | undefined
    let running: Promise<void> | undefined
    let again = false
    let againKeepalive = false
    const report = (next: SaveState) => {
      if (!disposed) setState(next)
    }
    const pending = () => {
      const now = comparable(documentOf(api.getState()))
      return now !== saved && now !== refused
    }
    // A refusal stops automatic retries, but the edit is still unsaved.
    const dirty = () => comparable(documentOf(api.getState())) !== saved

    const run = async (keepalive: boolean) => {
      const doc = documentOf(api.getState()) as Doc
      const key = comparable(doc)
      if (halted || key === saved || key === refused) return
      report({ kind: "saving" })
      try {
        const record = await ioRef.current.save(
          id,
          { document: doc, revision },
          { keepalive }
        )
        revision = record.revision
        saved = key
        refused = undefined
        attempt = 0
        ioRef.current.cache(record)
        api.setState({
          meta: { ...api.getState().meta, updated_at: record.updated_at },
        })
        report({ kind: "saved", at: Date.now() })
      } catch (caught) {
        const error = toApiError(caught)
        if (error.status === 409) {
          halted = true
          report({ kind: "conflict" })
        } else if (
          error.status !== undefined &&
          error.status >= 400 &&
          error.status < 500 &&
          error.status !== 408 &&
          error.status !== 429
        ) {
          refused = key
          report({
            kind: "refused",
            error: error.message,
            status: error.status,
          })
        } else {
          attempt += 1
          const delay = Math.min(
            RETRY_MS.most,
            RETRY_MS.first * 2 ** (attempt - 1)
          )
          report({
            kind: "retrying",
            error: error.message,
            next: Date.now() + delay,
          })
          clearTimeout(timer)
          timer = setTimeout(() => void save(), delay)
        }
      }
    }

    // One at a time; a change during a save is saved right after it.
    const save = (keepalive = false): Promise<void> => {
      timer = undefined
      if (running) {
        again = true
        againKeepalive ||= keepalive
        return running
      }
      running = run(keepalive).finally(() => {
        running = undefined
        if (again) {
          again = false
          const queuedKeepalive = againKeepalive
          againKeepalive = false
          void save(queuedKeepalive)
        }
      })
      return running
    }

    const unsubscribe = api.subscribe((now, before) => {
      if (
        now.nodes === before.nodes &&
        now.edges === before.edges &&
        now.meta === before.meta
      ) {
        return
      }
      const structural =
        now.nodes.length !== before.nodes.length ||
        now.edges.length !== before.edges.length
      clearTimeout(timer)
      timer = setTimeout(() => void save(), structural ? 0 : PAUSE_MS)
    })

    // Never race a request whose next revision we have not received yet.
    // If the page stays alive, the queued save uses that returned revision.
    const onPageHide = () => {
      if (halted || (!running && !pending())) return
      clearTimeout(timer)
      void save(true)
    }
    // Browser unload cannot guarantee that an outstanding save completes.
    const onBeforeUnload = (event: BeforeUnloadEvent) => {
      if (running || dirty()) {
        event.preventDefault()
        event.returnValue = ""
      }
    }
    window.addEventListener("pagehide", onPageHide)
    window.addEventListener("beforeunload", onBeforeUnload)

    const current = async () => {
      const record = await ioRef.current.fetch(id)
      ioRef.current.cache(record)
      return record
    }
    controls.current = {
      saveNow: () => {
        refused = undefined
        clearTimeout(timer)
        void save()
      },
      loadTheirs: async () => {
        const record = await current()
        api.getState().replaceDocument(record.document)
        api.setState({
          meta: { ...api.getState().meta, updated_at: record.updated_at },
        })
        revision = record.revision
        saved = comparable(documentOf(api.getState()))
        halted = false
        refused = undefined
        report({ kind: "saved", at: Date.parse(record.updated_at) })
      },
      keepMine: async () => {
        const record = await current()
        revision = record.revision
        halted = false
        refused = undefined
        await save()
      },
      flush: async () => {
        refused = undefined
        clearTimeout(timer)
        await save()
        while (running) await running
        return halted || refused !== undefined || pending() ? null : revision
      },
    }

    return () => {
      disposed = true
      unsubscribe()
      window.removeEventListener("pagehide", onPageHide)
      window.removeEventListener("beforeunload", onBeforeUnload)
      clearTimeout(timer)
      // Leaving the builder: what's left is saved after it's gone.
      if (!halted && (running || pending())) void save()
    }
  }, [api, start])

  return React.useMemo(
    () => ({
      state,
      saveNow: () => controls.current?.saveNow(),
      loadTheirs: async () => controls.current?.loadTheirs(),
      keepMine: async () => controls.current?.keepMine(),
      flush: async () => (controls.current ? controls.current.flush() : null),
    }),
    [state]
  )
}
