import {
  useMutation,
  useQuery,
  useQueryClient,
  type QueryClient,
} from "@tanstack/react-query"

import { createResource } from "@/lib/api/resource"
import { api } from "@/lib/api-instance"
import type { Audited, Scope } from "@/lib/hierarchy"

/*
 * Access control data from the admin API: permissions (`resource:action`),
 * roles that grant them, and who holds which role where. Anyone signed in
 * can read permissions and roles; changing them takes `permissions:*` and
 * `roles:*` on the site. Who holds what in a scope takes `members:read`
 * there, and assigning `members:update`.
 */

/* -------------------------------------------------------------------------- */
/* Permissions                                                                */
/* -------------------------------------------------------------------------- */

export type Permission = Audited & {
  id: number
  /** `resource:action`, e.g. `workflows:run`; either part may be `*`. */
  key: string
  resource: string
  action: string
  description: string
}

export type PermissionInput = { key: string; description: string }

export const permissions = createResource<
  Permission,
  { create: PermissionInput; update: Partial<PermissionInput> }
>({
  api,
  path: "/permissions",
  key: "permissions",
  label: "permission",
})

/* -------------------------------------------------------------------------- */
/* Roles                                                                      */
/* -------------------------------------------------------------------------- */

/** Where a role is assigned: a site role applies in every organization. */
export type RoleLevel = "site" | "org"

export const ROLE_LEVELS: { value: RoleLevel; label: string }[] = [
  { value: "site", label: "Site" },
  { value: "org", label: "Organization" },
]

export const levelLabel = (level: string) =>
  ROLE_LEVELS.find((option) => option.value === level)?.label ?? level

/** The part of a role key after the level: lowercase, digits and `_`. */
export const ROLE_NAME_PATTERN = /^[a-z][a-z0-9_]*$/

/** `resource:action`, either part lowercase (with `_`) or `*`. */
export const PERMISSION_KEY_PATTERN =
  /^(?:[a-z][a-z0-9_]*|\*):(?:[a-z][a-z0-9_]*|\*)$/

export type Role = Audited & {
  /** `<level>:<name>`, e.g. `org:admin`; never changes. */
  key: string
  level: RoleLevel
  name: string
  description: string
  permissions: Permission[]
  /** How many times it's assigned, across every scope. */
  member_count: number
}

export type RoleCreate = {
  key: string
  name: string
  description: string
  permission_ids: number[]
}
export type RoleUpdate = Partial<Omit<RoleCreate, "key">>

// Roles embed their permissions, so their queries live under the
// permissions' keys: editing or deleting a permission refreshes them too.
export const roleKeys = {
  all: [...permissions.keys.all, "roles"] as const,
  list: () => [...roleKeys.all, "list"] as const,
}

// Assignments change who holds a role (the roles' member counts), and maybe
// what the signed-in user may do.
const refreshAssignments = (queryClient: QueryClient) =>
  Promise.all([
    queryClient.invalidateQueries({ queryKey: memberKeys.all }),
    queryClient.invalidateQueries({ queryKey: roleKeys.all }),
    queryClient.invalidateQueries({ queryKey: ["access"] }),
  ])

/** Every role with its permissions, ordered by key. */
export function useRoles() {
  return useQuery({
    queryKey: roleKeys.list(),
    queryFn: ({ signal }) => api.get<Role[]>("/roles", { signal }),
  })
}

type MutationMeta = { meta?: { silent?: boolean; errorTitle?: string } }

export function useCreateRole({ meta }: MutationMeta = {}) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (input: RoleCreate) =>
      api.post<Role, RoleCreate>("/roles", input),
    meta: { errorTitle: "Couldn't create the role", ...meta },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: roleKeys.all }),
  })
}

export function useUpdateRole({ meta }: MutationMeta = {}) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ key, data }: { key: string; data: RoleUpdate }) =>
      api.patch<Role, RoleUpdate>(`/roles/${encodeURIComponent(key)}`, data),
    meta: { errorTitle: "Couldn't save the role", ...meta },
    // Changing what a role grants can change what the user may do.
    onSuccess: () =>
      Promise.all([
        queryClient.invalidateQueries({ queryKey: roleKeys.all }),
        queryClient.invalidateQueries({ queryKey: ["access"] }),
      ]),
  })
}

/** Deleting a role also revokes every assignment of it. */
export function useDeleteRole({ meta }: MutationMeta = {}) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (key: string) =>
      api.delete<void>(`/roles/${encodeURIComponent(key)}`),
    meta: { errorTitle: "Couldn't delete the role", ...meta },
    onSuccess: () => refreshAssignments(queryClient),
  })
}

/* -------------------------------------------------------------------------- */
/* Members: who holds which role where                                        */
/* -------------------------------------------------------------------------- */

/** One role assignment: a subject holds a role in a scope and below. */
export type Member = Audited & {
  /** A user's ID, or a service's. */
  subject_id: string
  role: string
  /** Where it was assigned. */
  scope: Scope
}

export type MemberSpan = {
  /** Also the roles assigned above that apply in the scope. */
  above?: boolean
  /** Also the roles assigned in the scopes inside it (the site's organizations). */
  below?: boolean
}

export const memberKeys = {
  all: ["members"] as const,
  scope: (scope: Scope, span: MemberSpan) =>
    [...memberKeys.all, scope, span] as const,
}

/** The role assignments in a scope; `span` widens it to what's active there. */
export function useScopeMembers(
  scope: Scope,
  span: MemberSpan = {},
  { enabled = true }: { enabled?: boolean } = {}
) {
  return useQuery({
    queryKey: memberKeys.scope(scope, span),
    queryFn: ({ signal }) =>
      api.get<Member[]>(`/scopes/${scope}/members`, { params: span, signal }),
    enabled,
  })
}

type Assignment = { scope: Scope; subjectId: string; role: string }

const assignmentPath = ({ scope, subjectId, role }: Assignment) =>
  `/scopes/${scope}/members/${encodeURIComponent(subjectId)}/roles/${encodeURIComponent(role)}`

/** Give people a role in a scope; one request each, all or none reported. */
export function useAssignRoles({ meta }: MutationMeta = {}) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (assignments: Assignment[]) =>
      Promise.all(assignments.map((a) => api.put<Member>(assignmentPath(a)))),
    meta: { errorTitle: "Couldn't assign the role", ...meta },
    // Some may have succeeded before one failed: refresh either way.
    onSettled: () => refreshAssignments(queryClient),
  })
}

export function useRevokeRole({ meta }: MutationMeta = {}) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (assignment: Assignment) =>
      api.delete<void>(assignmentPath(assignment)),
    meta: { errorTitle: "Couldn't revoke the role", ...meta },
    onSuccess: () => refreshAssignments(queryClient),
  })
}

/* -------------------------------------------------------------------------- */
/* Your organizations                                                         */
/* -------------------------------------------------------------------------- */

/** An organization you work in, and your roles in it. */
export type MyOrganization = {
  id: string
  name: string
  description: string
  /** Your roles in the organization, e.g. `org:member`. */
  roles: string[]
}

/**
 * The organizations you're a member of: those you hold a role in. A site
 * role doesn't make you one. Only members open an organization's workspace.
 * Under the members' keys, so role changes refresh it.
 */
export function useMyOrganizations() {
  return useQuery({
    queryKey: [...memberKeys.all, "me", "organizations"],
    queryFn: ({ signal }) =>
      api.get<MyOrganization[]>("/me/organizations", { signal }),
  })
}
