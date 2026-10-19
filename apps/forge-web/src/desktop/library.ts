import * as React from "react"
import { isTauri } from "@tauri-apps/api/core"
import { LazyStore } from "@tauri-apps/plugin-store"

import type { DesktopAgentLink } from "@/features/agents/lib/desktop-link"

/*
 * The agents added to the desktop app, and the one open: kept by the app
 * (Tauri's store plugin, `agents.json` in the app's data folder), or in
 * this browser's storage when the screen runs outside the app (its dev
 * server in a browser).
 */

/**
 * An agent added to the desktop app: one of the signed-in person's
 * organizations' published agents, run as them (`member`), or a chat page,
 * by its link (`page`, from a page's "Open in the desktop app").
 */
export type DesktopAgent = MemberAgent | PageAgent

/** A published agent of the person's organization, run as them through the runtime. */
export type MemberAgent = {
  kind: "member"
  /** Its ID (`ca_…`): the runtime runs its latest published version. */
  id: string
  organizationId: string
  organizationName: string
  /** The API it's on: `https://forge.example.com/api/v1`. */
  server: string
  /** Its name when it was added; the list of agents has its current one. */
  name: string
  /** When it was added (ISO 8601). */
  added: string
}

/** A chat page, by its link: its link is the credential, and it runs as a visitor. */
export type PageAgent = {
  /** Agents added before members could be: pages, with no kind. */
  kind?: "page"
  /** The page's link token (`cp_…`). */
  token: string
  server: string
  /** Its name when it was added; the page shows its current one. */
  name: string
  added: string
}

/** An agent's key in the library: a page's token, or a member agent's ID on its server. */
export const keyOf = (agent: DesktopAgent): string =>
  agent.kind === "member" ? `${agent.id} ${agent.server}` : agent.token

type Library = { agents: DesktopAgent[]; open?: string }

const KEY = "library"
const BROWSER_KEY = "forge.desktop.library"
const EMPTY: Library = { agents: [] }

const isText = (value: unknown): value is string => typeof value === "string"

const isAgent = (value: unknown): value is DesktopAgent => {
  if (typeof value !== "object" || value === null) return false
  const agent = value as Record<string, unknown>
  if (!isText(agent.server) || !isText(agent.name)) return false
  if (agent.kind === "member")
    return (
      isText(agent.id) &&
      isText(agent.organizationId) &&
      isText(agent.organizationName)
    )
  return (
    (agent.kind === undefined || agent.kind === "page") && isText(agent.token)
  )
}

const libraryOf = (value: unknown): Library => {
  if (typeof value !== "object" || value === null) return EMPTY
  const { agents, open } = value as Partial<Library>
  return {
    agents: Array.isArray(agents) ? agents.filter(isAgent) : [],
    open: typeof open === "string" ? open : undefined,
  }
}

/** Where the library is kept: the app's store, or this browser's storage. */
type Keeper = {
  read: () => Promise<Library>
  write: (library: Library) => Promise<void>
}

const appKeeper = (): Keeper => {
  const store = new LazyStore("agents.json", { defaults: {}, autoSave: false })
  return {
    read: async () => libraryOf(await store.get(KEY)),
    write: async (library) => {
      await store.set(KEY, library)
      await store.save()
    },
  }
}

const browserKeeper: Keeper = {
  read: async () => {
    try {
      return libraryOf(JSON.parse(localStorage.getItem(BROWSER_KEY) ?? "null"))
    } catch {
      return EMPTY
    }
  },
  write: async (library) => {
    try {
      localStorage.setItem(BROWSER_KEY, JSON.stringify(library))
    } catch {
      // Storage blocked: kept until the window closes.
    }
  },
}

const keeper: Keeper = isTauri() ? appKeeper() : browserKeeper

/** The library, and the agent a link to the app offers. */
type State = {
  library: Library
  /** Whether the first read finished: links wait for it. */
  loaded: boolean
  /** A link's agent, until it's added or turned down. */
  offered?: DesktopAgentLink
  /** A link that came before the first read finished. */
  waiting?: DesktopAgentLink
}

const isFor = (link: DesktopAgentLink) => (agent: DesktopAgent) =>
  agent.kind !== "member" &&
  agent.token === link.token &&
  agent.server === link.server

/** A link's agent: opened when it's here already, offered otherwise. */
const settle = (state: State, link: DesktopAgentLink): State =>
  state.library.agents.some(isFor(link))
    ? { ...state, library: { ...state.library, open: link.token } }
    : { ...state, offered: link }

/** The first read finished: what it read (unless changed since), then any link waiting. */
const loadedWith = (state: State, read: Library | undefined): State => {
  const { waiting, ...rest } = state
  const next = { ...rest, loaded: true, library: read ?? state.library }
  return waiting ? settle(next, waiting) : next
}

/**
 * The added agents and the one open, read once and written after every
 * change, and the agent a link to the app offers (`receive`).
 * `loaded` is false until the first read finishes.
 */
export function useLibrary() {
  const [state, setState] = React.useState<State>({
    library: EMPTY,
    loaded: false,
  })
  const [failure, setFailure] = React.useState<Error>()
  // Whether the person changed it: what was only read isn't written back.
  const changed = React.useRef(false)

  React.useEffect(() => {
    let current = true
    keeper
      .read()
      .then((read) => {
        if (current)
          setState((state) =>
            loadedWith(state, changed.current ? undefined : read)
          )
      })
      .catch((error: unknown) => {
        if (!current) return
        setFailure(toError(error))
        setState((state) => loadedWith(state, undefined))
      })
    return () => {
      current = false
    }
  }, [])

  const { library } = state
  React.useEffect(() => {
    if (!changed.current) return
    keeper.write(library).catch((error: unknown) => setFailure(toError(error)))
  }, [library])

  const change = React.useCallback((next: (library: Library) => Library) => {
    changed.current = true
    setState((state) => ({ ...state, library: next(state.library) }))
  }, [])

  const add = React.useCallback((agent: DesktopAgent) => {
    changed.current = true
    setState((state) => ({
      ...state,
      library: {
        // Added again: moved to the end, with what it shows now.
        agents: [
          ...state.library.agents.filter((a) => keyOf(a) !== keyOf(agent)),
          agent,
        ],
        open: keyOf(agent),
      },
      offered: undefined,
    }))
  }, [])
  const remove = React.useCallback(
    (key: string) =>
      change(({ agents, open }) => {
        const left = agents.filter((a) => keyOf(a) !== key)
        return {
          agents: left,
          open: open === key ? left[0] && keyOf(left[0]) : open,
        }
      }),
    [change]
  )
  const openAgent = React.useCallback(
    (key: string) => change((library) => ({ ...library, open: key })),
    [change]
  )
  /** A link to the app: its agent opened, or offered. */
  const receive = React.useCallback(
    (link: DesktopAgentLink) =>
      setState((state) =>
        state.loaded ? settle(state, link) : { ...state, waiting: link }
      ),
    []
  )
  const dismissOffer = React.useCallback(
    () => setState((state) => ({ ...state, offered: undefined })),
    []
  )

  const open =
    library.agents.find((a) => keyOf(a) === library.open) ?? library.agents[0]
  return {
    agents: library.agents,
    open,
    loaded: state.loaded,
    offered: state.offered,
    failure,
    add,
    remove,
    openAgent,
    receive,
    dismissOffer,
  }
}

const toError = (error: unknown) =>
  error instanceof Error ? error : new Error(String(error))
