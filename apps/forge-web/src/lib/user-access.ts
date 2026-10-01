import type {RefObject} from "react"
import {createStore} from "zustand"
import {useShallow} from "zustand/react/shallow"

import {createContextStore} from "@/lib/context-store"

/*
 * The signed-in user's roles and permissions, loaded from a Casbin backend.
 *
 * Casbin stays the authority: it enforces every request on the server. The
 * browser gets the user's effective roles and permissions once (Casbin's
 * GetImplicitRolesForUser / GetImplicitPermissionsForUser) and answers
 * "can they?" locally and synchronously, so screens can show, hide or
 * disable things without waiting.
 */

/** One permission, like a Casbin `p` policy line (obj, act). */
export type Permission = {
    /** The resource: exact, `*`, or a key pattern (`projects/*`, `/projects/:id`). */
    object: string
    /** The action: exact, `*`, or alternatives (`read|write`, `(read)|(write)`). */
    action: string
    /** The domain / tenant, for RBAC-with-domains models. */
    domain?: string
    /** Casbin's policy effect. A matching deny wins over any allow. */
    effect?: "allow" | "deny"
}

/** What the backend returns for the signed-in user. */
export type UserAccess = {
    /** Every role the user has, inherited ones included. */
    roles: string[]
    permissions: Permission[]
}

export type AccessRequest = { object: string; action: string; domain?: string }

/** Decides whether a permission covers a request (Casbin's matcher). */
export type PermissionMatcher = (
    request: AccessRequest,
    permission: Permission
) => boolean

/** `[object, action]` or `[object, action, domain]`, in Casbin's order. */
export type PermissionCheck =
    | readonly [object: string, action: string]
    | readonly [object: string, action: string, domain: string]

/* -------------------------------------------------------------------------- */
/* Casbin policies                                                            */
/* -------------------------------------------------------------------------- */

export type CasbinPolicyField = "sub" | "dom" | "obj" | "act" | "eft"

/**
 * Turns Casbin policy rows into permissions. `fields` is your model's
 * `policy_definition` order: `["sub", "obj", "act"]` by default,
 * `["sub", "dom", "obj", "act"]` with domains, add `"eft"` for deny rules.
 *
 * ```ts
 * // Backend: roles = get_implicit_roles_for_user(user),
 * //          policies = get_implicit_permissions_for_user(user)
 * fromCasbin({ roles, policies })
 * ```
 */
export function fromCasbin(
    {roles = [], policies = []}: { roles?: string[]; policies?: string[][] },
    fields: CasbinPolicyField[] = ["sub", "obj", "act"]
): UserAccess {
    const at = (name: CasbinPolicyField) => fields.indexOf(name)
    const [obj, act, dom, eft] = [at("obj"), at("act"), at("dom"), at("eft")]
    return {
        roles: [...new Set(roles)],
        permissions: policies
            .filter((row) => row[obj] !== undefined && row[act] !== undefined)
            .map((row) => ({
                object: row[obj]!,
                action: row[act]!,
                ...(dom >= 0 && row[dom] !== undefined && {domain: row[dom]}),
                ...(eft >= 0 && row[eft] === "deny" && {effect: "deny" as const}),
            })),
    }
}

/* -------------------------------------------------------------------------- */
/* Matching                                                                   */
/* -------------------------------------------------------------------------- */

const patterns = new Map<string, RegExp>()

// Casbin's keyMatch / keyMatch2 / keyMatch3: `*` is anything, `:id` and
// `{id}` are one path segment.
function toPattern(pattern: string) {
    let regex = patterns.get(pattern)
    if (!regex) {
        const source = pattern
            .replace(/[.+?^$()[\]\\|]/g, "\\$&")
            .replace(/\*/g, ".*")
            .replace(/:[A-Za-z0-9_]+|\{[A-Za-z0-9_]+\}/g, "[^/]+")
        regex = new RegExp(`^${source}$`)
        patterns.set(pattern, regex)
    }
    return regex
}

/** Casbin-style key matching: exact, `*`, or `keyMatch`-style patterns. */
export function matchKey(pattern: string, value: string) {
    if (pattern === "*" || pattern === value) return true
    return /[*:{]/.test(pattern) && toPattern(pattern).test(value)
}

/** Exact, `*`, or alternatives like `read|write` / `(read)|(write)`. */
export function matchAction(pattern: string, action: string) {
    if (pattern === "*" || pattern === action) return true
    return (
        pattern.includes("|") &&
        pattern
            .split("|")
            .map((part) => part.replace(/[()^$\s]/g, ""))
            .includes(action)
    )
}

/**
 * The default matcher: key patterns for objects and domains, alternatives
 * for actions. A check without a domain matches permissions in any domain.
 */
export const defaultMatcher: PermissionMatcher = (request, permission) =>
    matchKey(permission.object, request.object) &&
    matchAction(permission.action, request.action) &&
    (request.domain === undefined ||
        permission.domain === undefined ||
        matchKey(permission.domain, request.domain))

/** Allowed when a permission matches and no matching permission denies. */
export function evaluate(
    permissions: readonly Permission[],
    request: AccessRequest,
    matcher: PermissionMatcher = defaultMatcher
) {
    let allowed = false
    for (const permission of permissions) {
        if (!matcher(request, permission)) continue
        if (permission.effect === "deny") return false
        allowed = true
    }
    return allowed
}

/* -------------------------------------------------------------------------- */
/* Store                                                                      */
/* -------------------------------------------------------------------------- */

export type AccessStatus = "loading" | "ready" | "error"

export type AccessLoader = (options: {
    signal: AbortSignal
}) => Promise<UserAccess>

export type UserAccessState = UserAccess & {
    /** `loading` until the first load finishes; stays `ready` while refreshing. */
    status: AccessStatus
    /** True while `reload()` refreshes access that's already loaded. */
    refreshing: boolean
    error: unknown
    /** The domain / tenant checks use when they don't name one. */
    domain: string | undefined
    matcher: PermissionMatcher
    /** Can the user do `action` on `object`? For event handlers; components use `useCan`. */
    can: (object: string, action: string, domain?: string) => boolean
    hasRole: (role: string) => boolean
    /** Loads access again, e.g. after the user's roles change. */
    reload: () => Promise<void>
    /** Cancels a load in progress. */
    abort: () => void
}

export type UserAccessStoreOptions = {
    loader: RefObject<AccessLoader | undefined>
    initial?: UserAccess
    domain?: string
    matcher?: PermissionMatcher
}

function createUserAccessStore({
                                   loader,
                                   initial,
                                   domain,
                                   matcher = defaultMatcher,
                               }: UserAccessStoreOptions) {
    let controller: AbortController | undefined

    return createStore<UserAccessState>()((set, get) => ({
        roles: initial?.roles ?? [],
        permissions: initial?.permissions ?? [],
        status: initial ? "ready" : "loading",
        refreshing: false,
        error: undefined,
        domain,
        matcher,
        can: (object, action, requestDomain) => {
            const state = get()
            return evaluate(
                state.permissions,
                {object, action, domain: requestDomain ?? state.domain},
                state.matcher
            )
        },
        hasRole: (role) => get().roles.includes(role),
        abort: () => controller?.abort(),
        reload: async () => {
            const load = loader.current
            if (!load) return
            controller?.abort()
            const current = new AbortController()
            controller = current
            const loaded = get().status === "ready"
            set(
                loaded ? {refreshing: true} : {status: "loading", error: undefined}
            )
            try {
                const access = await load({signal: current.signal})
                if (current.signal.aborted) return
                set({
                    roles: [...new Set(access.roles)],
                    permissions: access.permissions,
                    status: "ready",
                    refreshing: false,
                    error: undefined,
                })
            } catch (error) {
                if (current.signal.aborted) return
                // A failed refresh keeps the access already loaded.
                set(loaded ? {refreshing: false, error} : {status: "error", error})
            }
        },
    }))
}

export const {
    Provider: UserAccessStoreProvider,
    useStore: useUserAccess,
    useStoreApi: useUserAccessApi,
    // For components that work with or without a UserAccessProvider.
    Context: UserAccessContext,
} = createContextStore(createUserAccessStore, {name: "UserAccess"})

/* -------------------------------------------------------------------------- */
/* Checks                                                                     */
/* -------------------------------------------------------------------------- */

export type GuardOptions = {
    /** A required permission: `[object, action]` or `[object, action, domain]`. */
    permission?: PermissionCheck
    /** Several permissions; `mode` decides whether all or any are needed. */
    permissions?: readonly PermissionCheck[]
    /** A required role (inherited roles count). */
    role?: string
    /** Several roles; `mode` decides whether all or any are needed. */
    roles?: readonly string[]
    /** "all" (default) or "any" within `permissions` and within `roles`. */
    mode?: "all" | "any"
    /** Any other rule, with the loaded access. */
    when?: (access: Pick<UserAccessState, "can" | "hasRole" | "roles">) => boolean
}

/** Every listed kind of requirement must pass; `mode` applies within a list. */
export function checkAccess(state: UserAccessState, options: GuardOptions) {
    const {
        permission,
        permissions = [],
        role,
        roles = [],
        mode = "all",
    } = options
    const combine = (results: boolean[]) =>
        results.length === 0 ||
        (mode === "all" ? results.every(Boolean) : results.some(Boolean))
    const checks = permission ? [permission, ...permissions] : permissions
    const roleList = role ? [role, ...roles] : roles
    return (
        combine(
            checks.map(([object, action, domain]) =>
                state.can(object, action, domain)
            )
        ) &&
        combine(roleList.map((name) => state.roles.includes(name))) &&
        (options.when ? options.when(state) : true)
    )
}

/**
 * Whether the user passes a guard, and the load status — for disabling
 * rather than hiding: `const { allowed } = useGuard({ permission: [...] })`.
 * Not allowed until access has loaded.
 */
export function useGuard(options: GuardOptions) {
    return useUserAccess(
        useShallow((state) => ({
            allowed: state.status === "ready" && checkAccess(state, options),
            status: state.status,
        }))
    )
}

/** Can the user do `action` on `object`? Re-renders when the answer changes. */
export function useCan(object: string, action: string, domain?: string) {
    return useUserAccess(
        (state) =>
            state.status === "ready" &&
            evaluate(
                state.permissions,
                {object, action, domain: domain ?? state.domain},
                state.matcher
            )
    )
}

/** Does the user have `role` (inherited roles count)? */
export function useHasRole(role: string) {
    return useUserAccess(
        (state) => state.status === "ready" && state.roles.includes(role)
    )
}
