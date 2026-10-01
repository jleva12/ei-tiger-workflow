import * as React from "react"

import { StatGrid, Stat } from "@/components/forge/activity"
import {
  createColumnHelper,
  DataTable,
  type InitialTableState,
} from "@/components/forge/data-table"
import { ErrorCallout } from "@/components/forge/feedback"
import { Chip } from "@/components/forge/status"
import { Button } from "@/components/ui/button"
import {
  useAssignRoles,
  useRevokeRole,
  useRoles,
  useScopeMembers,
  type Member,
} from "@/lib/access"
import type { Scope } from "@/lib/hierarchy"
import { userName, users, type User } from "@/lib/users"
import { AssignRoleDialog, type AssignTarget } from "./assign-role-dialog"
import { DeleteDialog } from "./delete-dialog"
import { ADMIN_TABLE_FEATURES, DATE_TIME, PIN_ACTIONS } from "./table-config"
import { EmptyRows, RowMenu } from "./table-parts"
import { UserAvatar } from "./user-picker"

// The dialogs show failures themselves, so skip the error toast.
const SILENT = { meta: { silent: true } }

const DEPTH = { site: 0, org: 1 } as const
const depth = (scope: Scope) =>
  DEPTH[scope === "site" ? "site" : (scope.split(":")[0] as keyof typeof DEPTH)]

/** A user's name, or the subject ID when they aren't in Users. */
const userNameOr = (user: User | undefined, subjectId: string) =>
  user ? userName(user) : subjectId

/** An assignment with what the table shows about it. */
type MemberRow = Member & {
  id: string
  person: User | undefined
  roleName: string
  where: string
  /** Assigned above the scope, so managed there, not here. */
  inherited: boolean
  assignedBy: string
}

const INITIAL_STATE: InitialTableState = {
  columnVisibility: { assigned_by: false },
  columnPinning: PIN_ACTIONS,
}

// Revoking reaches the cells through context, so the columns stay the same
// from render to render.
const RevokeContext = React.createContext<
  ((member: MemberRow) => void) | undefined
>(undefined)

const helper = createColumnHelper<MemberRow>()
const COLUMNS = helper.columns([
  helper.accessor((row) => userNameOr(row.person, row.subject_id), {
    id: "person",
    header: "Person",
    size: 260,
    meta: { label: "Person" },
    cell: ({ row: { original: row } }) =>
      row.person ? (
        <span className="flex min-w-0 items-center gap-2">
          <UserAvatar user={row.person} />
          <span className="flex min-w-0 flex-col">
            <span className="truncate font-medium text-foreground">
              {userName(row.person)}
            </span>
            <span className="truncate text-2xs text-muted-foreground">
              {row.person.email}
            </span>
          </span>
        </span>
      ) : (
        <span className="flex min-w-0 flex-col">
          <span className="truncate font-mono text-foreground">
            {row.subject_id}
          </span>
          <span className="text-2xs text-subtle">Not in Users</span>
        </span>
      ),
  }),
  helper.accessor("roleName", {
    header: "Role",
    size: 220,
    meta: { label: "Role" },
    cell: ({ row: { original: row } }) => (
      <span className="flex min-w-0 flex-col">
        <span className="truncate text-foreground">{row.roleName}</span>
        <span className="truncate font-mono text-2xs text-muted-foreground">
          {row.role}
        </span>
      </span>
    ),
  }),
  helper.accessor("where", {
    header: "Where",
    size: 220,
    meta: { label: "Where" },
    cell: ({ row: { original: row } }) => (
      <span className="flex min-w-0 items-center gap-2">
        <span className="truncate">{row.where}</span>
        {row.inherited && <Chip>Inherited</Chip>}
      </span>
    ),
  }),
  helper.accessor("created_at", {
    header: "Assigned",
    size: 170,
    meta: { label: "Assigned", format: "datetime", formatOptions: DATE_TIME },
  }),
  helper.accessor("assignedBy", {
    id: "assigned_by",
    header: "Assigned by",
    size: 170,
    meta: { label: "Assigned by", cellClassName: "text-muted-foreground" },
  }),
  helper.display({
    id: "actions",
    header: () => <span className="sr-only">Actions</span>,
    size: 64,
    enableSorting: false,
    enableHiding: false,
    enableResizing: false,
    meta: { label: "Actions", align: "right" },
    cell: ({ row }) => <RowActions member={row.original} />,
  }),
])

function RowActions({ member }: { member: MemberRow }) {
  const onRevoke = React.useContext(RevokeContext)
  // Inherited roles are managed where they were assigned.
  if (member.inherited) return null
  return (
    <RowMenu
      label={`${userNameOr(member.person, member.subject_id)} as ${member.roleName}`}
      onDelete={onRevoke && (() => onRevoke(member))}
      deleteLabel="Revoke…"
    />
  )
}

type ScopeMembersProps = {
  scope: Scope
  /** Names the scope an assignment was made in: "Site", an organization's name… */
  scopeLabel: (scope: Scope) => string
  /** Where roles can be assigned from here. */
  targets: AssignTarget[]
  /** Offer assigning and revoking (`members:update` in the scope). */
  canManage: boolean
  /** Whether the assign dialog is open; the page's primary action opens it. */
  assigning: boolean
  onAssigningChange: (open: boolean) => void
  description?: string
}

/**
 * Everything active in a scope: every role that applies there, whether
 * assigned in it, inherited from above, or assigned inside it (its organizations),
 * with who holds it. Roles assigned here and inside can be revoked.
 */
export function ScopeMembers({
  scope,
  scopeLabel,
  targets,
  canManage,
  assigning,
  onAssigningChange,
  description,
}: ScopeMembersProps) {
  const list = useScopeMembers(scope, { above: true, below: true })
  const people = users.useList()
  const roles = useRoles()
  const assign = useAssignRoles(SILENT)
  const revoke = useRevokeRole(SILENT)
  const [revoking, setRevoking] = React.useState<MemberRow>()

  const rows = React.useMemo(() => {
    const byId = new Map((people.data ?? []).map((user) => [user.id, user]))
    const roleNames = new Map(
      (roles.data ?? []).map((role) => [role.key, role.name])
    )
    return (list.data ?? []).map((member): MemberRow => ({
      ...member,
      id: `${member.subject_id} ${member.role} ${member.scope}`,
      person: byId.get(member.subject_id),
      roleName: roleNames.get(member.role) ?? member.role,
      where: scopeLabel(member.scope),
      inherited: depth(member.scope) < depth(scope),
      assignedBy: byId.get(member.created_by)?.email ?? member.created_by,
    }))
  }, [list.data, people.data, roles.data, scope, scopeLabel])

  const inheritedCount = rows.filter((row) => row.inherited).length
  const peopleCount = new Set(rows.map((row) => row.subject_id)).size

  if (list.error && !list.data) {
    return (
      <ErrorCallout
        title="Couldn't load who has access"
        action={
          <Button
            variant="outline"
            size="sm"
            onClick={() => void list.refetch()}
          >
            Retry
          </Button>
        }
      >
        {list.error.message}
      </ErrorCallout>
    )
  }

  return (
    <div className="flex flex-col gap-5">
      <StatGrid>
        <Stat
          label="People with access"
          value={list.data ? peopleCount : "–"}
        />
        <Stat
          label="Roles assigned here"
          value={list.data ? rows.length - inheritedCount : "–"}
        />
        <Stat label="Inherited" value={list.data ? inheritedCount : "–"} />
      </StatGrid>
      <RevokeContext.Provider value={canManage ? setRevoking : undefined}>
        <DataTable
          className="rounded-none border-0"
          title="Access"
          description={description}
          columns={COLUMNS}
          data={rows}
          isLoading={list.isPending}
          getRowId={(row) => row.id}
          features={ADMIN_TABLE_FEATURES}
          initialState={INITIAL_STATE}
          stateKey="admin-scope-members"
          exportFileName="access"
          labels={{
            searchPlaceholder: "Search people, roles and organizations…",
          }}
          emptyState={
            rows.length === 0 ? (
              <EmptyRows
                message="Nobody has a role here yet."
                createLabel="Assign a role"
                onCreate={canManage ? () => onAssigningChange(true) : undefined}
              />
            ) : undefined
          }
        />
      </RevokeContext.Provider>
      <AssignRoleDialog
        open={assigning}
        onOpenChange={onAssigningChange}
        targets={targets}
        roles={roles.data ?? []}
        onSubmit={({ scope: where, role, subjectIds }) =>
          assign.mutateAsync(
            subjectIds.map((subjectId) => ({ scope: where, subjectId, role }))
          )
        }
      />
      <DeleteDialog
        open={revoking !== undefined}
        onClose={() => setRevoking(undefined)}
        title={`Revoke ${revoking?.roleName ?? "the role"}?`}
        description={`${revoking ? userNameOr(revoking.person, revoking.subject_id) : "They"} will lose what it grants in ${revoking?.where ?? "this scope"}.`}
        confirmLabel="Revoke role"
        onConfirm={() =>
          revoke.mutateAsync({
            scope: revoking!.scope,
            subjectId: revoking!.subject_id,
            role: revoking!.role,
          })
        }
      />
    </div>
  )
}
