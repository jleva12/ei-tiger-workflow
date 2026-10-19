import { useQuery } from "@tanstack/react-query"

import { createNestedResource } from "@/lib/api/resource"
import { api } from "@/lib/api-instance"
import type { Audited } from "@/lib/hierarchy"

/*
 * An organization's own agent permissions
 * (`/organizations/{org}/agent-access`): who may use its agents and run its
 * workflows. Apart from the site's roles and permissions (`@/lib/access`),
 * which decide what people see and do in Forge, the organization names its
 * own agent permissions (`resource:action`) and links each to company groups
 * (whoever's sign-in names one holds it) and to its own API keys. There are
 * no agent roles: the directory's groups say who's who. They count only in
 * the organization. Reading them takes `members:read` there; changing them,
 * `agent_access:manage`.
 */

const base = (organizationId: string) =>
  `/organizations/${encodeURIComponent(organizationId)}/agent-access`

/**
 * Which of the organization's agent permissions let someone use a chat
 * agent or run a workflow (`access` on its record), any one of them, for
 * every version at once. With none, anyone who may call the organization's
 * agents may; those who build its agents always may.
 */
export type DocumentAccess = {
  permission_ids: number[]
  updated_at: string
  updated_by: string
  updated_by_name: string | null
}

/* -------------------------------------------------------------------------- */
/* Agent permissions                                                          */
/* -------------------------------------------------------------------------- */

export type AgentPermission = Audited & {
  id: number
  /** `resource:action`, e.g. `invoices:approve`; either part may be `*`. */
  key: string
  resource: string
  action: string
  description: string
  /**
   * The company groups (from its external directory) linked to it: anyone
   * whose sign-in names one of them holds it in the organization.
   */
  groups: string[]
  /** The organization's API keys linked to it, for outside apps. */
  api_keys: { id: string; name: string }[]
}

export type AgentPermissionInput = {
  key: string
  description: string
  /** Replaces the company groups linked to it. */
  groups?: string[]
  /** Replaces the API keys linked to it, by ID. */
  api_keys?: string[]
}

export const agentPermissions = createNestedResource<
  AgentPermission,
  { organizationId: string },
  { create: AgentPermissionInput; update: Partial<AgentPermissionInput> }
>({
  api,
  key: "agent-access",
  path: ({ organizationId }) => `${base(organizationId)}/permissions`,
  label: "agent permission",
  updateMethod: "patch",
})

/* -------------------------------------------------------------------------- */
/* Company groups                                                             */
/* -------------------------------------------------------------------------- */

/**
 * A company group linked to the organization's agent permissions (from each
 * permission's `groups`), with what it's linked to.
 */
export type AgentGroupLink = Audited & {
  id: number
  group_name: string
  description: string
  permissions: AgentPermission[]
}

/** The company groups linked to the organization's agent permissions. */
export function useAgentGroups(organizationId: string) {
  return useQuery({
    // Under the permissions' keys: changing a permission's groups refreshes them.
    queryKey: [
      ...agentPermissions.scope({ organizationId }).keys.all,
      "groups",
    ],
    queryFn: ({ signal }) =>
      api.get<AgentGroupLink[]>(`${base(organizationId)}/groups`, { signal }),
  })
}
