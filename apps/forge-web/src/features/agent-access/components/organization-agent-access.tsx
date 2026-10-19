import { AgentPermissions } from "./agent-permissions"

/**
 * An organization's Agent access page: its own agent permissions, which its
 * chat agents and workflows ask for (each says which under "Who may use
 * it"), each linked to the company's groups (whoever's sign-in names one
 * holds it) and to its own API keys. There are no agent roles: the
 * directory's groups say who's who.
 */
export function OrganizationAgentAccess({
  organizationId,
  organizationName,
  canManage,
}: {
  organizationId: string
  organizationName: string
  /** Set them up (`agent_access:manage`); otherwise they're read only. */
  canManage: boolean
}) {
  return (
    <AgentPermissions
      organizationId={organizationId}
      organizationName={organizationName}
      canManage={canManage}
    />
  )
}
