import * as React from "react"
import {useQuery} from "@tanstack/react-query"

import {createResource} from "@/lib/api/resource"
import {api} from "@/lib/api-instance"
import {evaluate, fromCasbin, type UserAccess} from "@/lib/user-access"

/*
 * The hierarchy the admin API serves: the site, and its organizations. An
 * organization is listed and created at `/organizations` and read, edited and
 * deleted at `/organizations/:id`.
 */

/** Who created and last changed a record, and when (ISO 8601). */
export type Audited = {
    created_at: string
    created_by: string
    updated_at: string
    updated_by: string
}

/** An organization. */
export type Organization = Audited & {
    id: string
    name: string
    description: string
    /** Its Casbin domain, `org:<id>`. */
    domain: string
}

/** What creates or edits an organization. */
export type NodeInput = { name: string; description: string }

type NodeTypes = { create: NodeInput; update: Partial<NodeInput> }

export const organizations = createResource<Organization, NodeTypes>({
    api,
    path: "/organizations",
    key: "organizations",
    label: "organization",
})

/* -------------------------------------------------------------------------- */
/* Access in a scope                                                          */
/* -------------------------------------------------------------------------- */

/** A scope as the API names it. */
export type Scope = "site" | `org:${string}`

/** A permission key, `resource:action`, e.g. `workflows:run`. */
export type PermissionKey = `${string}:${string}`

type CasbinAccess = { roles?: string[]; policies?: string[][] }

const toAccess = (access: CasbinAccess): UserAccess => fromCasbin(access)

/**
 * What the signed-in user may do in one scope, from `GET /me/access?scope=`:
 * the Casbin roles they hold there (those assigned in the scopes above
 * included) and what those roles grant. The API enforces every request
 * itself; this decides which actions to offer.
 *
 * @example
 * const can = useScopeAccess(`org:${organizationId}`)
 * can("workflows:manage") // false until access has loaded
 */
export function useScopeAccess(scope: Scope | undefined) {
    const {data} = useQuery({
        queryKey: ["access", scope],
        queryFn: ({signal}) =>
            api.get<CasbinAccess>("/me/access", {params: {scope}, signal}),
        enabled: scope !== undefined,
        select: toAccess,
    })
    return React.useCallback(
        (permission: PermissionKey) => {
            if (!data) return false
            const [object = "", action = ""] = permission.split(":")
            return evaluate(data.permissions, {object, action})
        },
        [data]
    )
}
