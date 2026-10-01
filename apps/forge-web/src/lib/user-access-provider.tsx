import * as React from "react"

import {
  checkAccess,
  UserAccessStoreProvider,
  useUserAccess,
  useUserAccessApi,
  type AccessLoader,
  type GuardOptions,
  type PermissionMatcher,
  type UserAccess,
} from "@/lib/user-access"

export type UserAccessProviderProps = {
  children?: React.ReactNode
  /**
   * Fetches the signed-in user's roles and permissions. Called on mount and
   * by `reload()`; `signal` aborts when the provider unmounts or reloads.
   *
   * ```ts
   * load={async ({ signal }) =>
   *   fromCasbin((await api.get("/me/access", { signal })).data)}
   * ```
   */
  load: AccessLoader
  /** Access already known (e.g. bootstrapped with the page); skips the first load. */
  initial?: UserAccess
  /** The current domain / tenant, for RBAC-with-domains models. */
  domain?: string
  /** A custom Casbin matcher, when your model isn't key/wildcard matching. */
  matcher?: PermissionMatcher
  /**
   * Shown instead of the app until access has loaded. Without it the app
   * renders right away and each `Guard` shows its own `loading`.
   */
  fallback?: React.ReactNode
  /** Shown instead of the app when the first load fails. */
  errorFallback?: (error: unknown, retry: () => void) => React.ReactNode
}

function LoadAccess({ skip }: { skip: boolean }) {
  const api = useUserAccessApi()
  React.useEffect(() => {
    if (!skip) void api.getState().reload()
    return () => api.getState().abort()
  }, [api, skip])
  return null
}

function SyncOptions({
  domain,
  matcher,
}: Pick<UserAccessProviderProps, "domain" | "matcher">) {
  const api = useUserAccessApi()
  React.useEffect(() => {
    api.setState({ domain })
  }, [api, domain])
  React.useEffect(() => {
    if (matcher) api.setState({ matcher })
  }, [api, matcher])
  return null
}

function AccessGate({
  fallback,
  errorFallback,
  children,
}: Pick<UserAccessProviderProps, "fallback" | "errorFallback" | "children">) {
  const status = useUserAccess((state) => state.status)
  const error = useUserAccess((state) => state.error)
  const reload = useUserAccess((state) => state.reload)
  if (status === "loading" && fallback !== undefined) return <>{fallback}</>
  if (status === "error" && errorFallback) {
    return <>{errorFallback(error, () => void reload())}</>
  }
  return <>{children}</>
}

/**
 * Loads the signed-in user's roles and permissions from your backend (a
 * Casbin enforcer) once, and answers access checks from them for `Guard`,
 * `useCan`, `useHasRole` and `useGuard`. Give it a `key` of the user's id so
 * signing in as someone else starts fresh.
 *
 * The checks decide what the UI shows; Casbin still enforces every request
 * on the server.
 */
export function UserAccessProvider({
  children,
  load,
  initial,
  domain,
  matcher,
  fallback,
  errorFallback,
}: UserAccessProviderProps) {
  const loader = React.useRef(load)
  React.useEffect(() => {
    loader.current = load
  })

  return (
    <UserAccessStoreProvider initial={{ loader, initial, domain, matcher }}>
      <LoadAccess skip={initial !== undefined} />
      <SyncOptions domain={domain} matcher={matcher} />
      <AccessGate fallback={fallback} errorFallback={errorFallback}>
        {children}
      </AccessGate>
    </UserAccessStoreProvider>
  )
}

export type GuardProps = GuardOptions & {
  children?: React.ReactNode
  /** Rendered when the user doesn't pass. Default: nothing. */
  fallback?: React.ReactNode
  /** Rendered while access is loading. Default: nothing. */
  loading?: React.ReactNode
}

/**
 * Renders its children only for users with the required permissions and
 * roles; otherwise `fallback`. Permissions use Casbin's `[object, action]`
 * order.
 *
 * ```tsx
 * <Guard permission={["projects", "delete"]}>
 *   <DeleteProjectButton />
 * </Guard>
 * <Guard role="admin" fallback={<PageEmpty illustration="locked" … />}>
 *   <BillingPage />
 * </Guard>
 * <Guard permissions={[["tasks", "create"], ["tasks", "update"]]} mode="any">…</Guard>
 * ```
 */
export function Guard({
  children,
  fallback = null,
  loading = null,
  ...options
}: GuardProps) {
  const status = useUserAccess((state) => state.status)
  const allowed = useUserAccess(
    (state) => state.status === "ready" && checkAccess(state, options)
  )
  if (status === "loading") return <>{loading}</>
  return <>{allowed ? children : fallback}</>
}
