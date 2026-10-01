import { createStore } from "zustand"
import { createJSONStorage, persist } from "zustand/middleware"

import { createContextStore } from "@/lib/context-store"

/**
 * Where the signed-in user is working: `admin` (site administration:
 * organizations, users and access; site administrators only) or one of their
 * organizations, `org:<id>`. The switcher at the top of the sub nav changes
 * it, and so does opening a scope's page; home opens the last one.
 */
export type WorkspaceScope = "admin" | `org:${string}`

/** The role that opens the admin scope: site administration. */
export const SITE_ADMIN = "site:admin"

type WorkspaceScopeState = {
  /** The last scope worked in; null before the first. */
  scope: WorkspaceScope | null
  setScope: (scope: WorkspaceScope) => void
}

const isScope = (value: unknown): value is WorkspaceScope =>
  value === "admin" || (typeof value === "string" && /^org:\S+$/.test(value))

/** The organization a scope is, if it's one. */
export const organizationOf = (scope: WorkspaceScope | null) =>
  scope?.startsWith("org:") ? scope.slice("org:".length) : undefined

/** Site administration's pages, all under /admin. */
export const isAdminPath = (pathname: string) =>
  pathname === "/admin" || pathname.startsWith("/admin/")

/** The organization whose pages a path is in (/organizations/<id>…), if it's one's. */
export const organizationInPath = (pathname: string) =>
  /^\/organizations\/([^/]+)/.exec(pathname)?.[1]

export const {
  Provider: WorkspaceScopeProvider,
  useStore: useWorkspaceScope,
  useStoreApi: useWorkspaceScopeApi,
} = createContextStore(
  () =>
    createStore<WorkspaceScopeState>()(
      persist(
        (set) => ({
          scope: null,
          setScope: (scope) => set({ scope }),
        }),
        {
          name: "forge.workspace-scope",
          version: 2,
          storage: createJSONStorage(() => localStorage),
          partialize: ({ scope }) => ({ scope }),
          // A stale or edited entry falls back to no scope.
          merge: (persisted, current) => {
            const scope = (persisted as { scope?: unknown } | undefined)?.scope
            return { ...current, scope: isScope(scope) ? scope : null }
          },
        }
      )
    ),
  { name: "WorkspaceScope" }
)
