import * as React from "react"
import { UserShield01Icon } from "@hugeicons/core-free-icons"
import { createFileRoute } from "@tanstack/react-router"

import { DeleteDialog } from "@/features/admin/components/delete-dialog"
import { RoleSheet } from "@/features/admin/components/role-sheet"
import {
  ADMIN_TABLE_FEATURES,
  DATE_TIME,
  PIN_ACTIONS,
} from "@/features/admin/components/table-config"
import { EmptyRows, RowMenu } from "@/features/admin/components/table-parts"
import { PrimaryAction } from "@/components/forge/app-shell"
import {
  createColumnHelper,
  DataTable,
  type InitialTableState,
} from "@/components/forge/data-table"
import { ErrorCallout } from "@/components/forge/feedback"
import { ShellHeaderActions, useShellPage } from "@/components/forge/shell"
import { Button } from "@/components/ui/button"
import {
  levelLabel,
  useCreateRole,
  useDeleteRole,
  useRoles,
  useUpdateRole,
  type Role,
} from "@/lib/access"
import { useScopeAccess } from "@/lib/hierarchy"
import { useActorLabels } from "@/lib/users"

export const Route = createFileRoute("/admin/roles")({
  // Site administration: the /admin layout lets only site administrators in.
  component: RolesPage,
})

// The sheet and dialog show failures themselves, so skip the error toast.
const SILENT = { meta: { silent: true } }

const INITIAL_STATE: InitialTableState = {
  columnVisibility: { updated_by: false },
  columnPinning: PIN_ACTIONS,
}

type RowActionHandlers = {
  onEdit?: (role: Role) => void
  onDelete?: (role: Role) => void
}

// The handlers reach the cells through context, so the columns stay the same
// from render to render.
const RowActionsContext = React.createContext<RowActionHandlers>({})

const plural = (count: number, noun: string) =>
  count === 0 ? `No ${noun}s` : `${count} ${noun}${count === 1 ? "" : "s"}`

const helper = createColumnHelper<Role>()
const COLUMNS = helper.columns([
  helper.accessor("name", {
    header: "Role",
    size: 300,
    meta: { label: "Role" },
    cell: ({ row }) => (
      <span className="flex min-w-0 flex-col">
        <span className="truncate font-medium text-foreground">
          {row.original.name}
        </span>
        {row.original.description && (
          <span className="truncate text-2xs text-muted-foreground">
            {row.original.description}
          </span>
        )}
      </span>
    ),
  }),
  helper.accessor((role) => levelLabel(role.level), {
    id: "level",
    header: "Assigned in",
    size: 130,
    meta: { label: "Assigned in" },
  }),
  helper.accessor("key", {
    header: "Key",
    size: 170,
    meta: { label: "Key", cellClassName: "font-mono text-muted-foreground" },
  }),
  helper.accessor("member_count", {
    header: "Members",
    size: 100,
    meta: { label: "Members", format: "integer" },
  }),
  helper.accessor((role) => role.permissions.length, {
    id: "permissions",
    header: "Permissions",
    size: 140,
    meta: { label: "Permissions", cellClassName: "text-muted-foreground" },
    cell: ({ getValue }) => plural(getValue(), "permission"),
  }),
  helper.accessor("updated_at", {
    header: "Updated",
    size: 170,
    meta: { label: "Updated", format: "datetime", formatOptions: DATE_TIME },
  }),
  helper.accessor("updated_by", {
    header: "Updated by",
    size: 160,
    meta: { label: "Updated by", cellClassName: "text-muted-foreground" },
  }),
  helper.display({
    id: "actions",
    header: () => <span className="sr-only">Actions</span>,
    size: 64,
    enableSorting: false,
    enableHiding: false,
    enableResizing: false,
    meta: { label: "Actions", align: "right" },
    cell: ({ row }) => <RowActions role={row.original} />,
  }),
])

function RowActions({ role }: { role: Role }) {
  const { onEdit, onDelete } = React.useContext(RowActionsContext)
  return (
    <RowMenu
      label={role.name}
      onEdit={onEdit && (() => onEdit(role))}
      onDelete={onDelete && (() => onDelete(role))}
    />
  )
}

function RolesPage() {
  const can = useScopeAccess("site")
  const list = useRoles()
  const create = useCreateRole(SILENT)
  const update = useUpdateRole(SILENT)
  const remove = useDeleteRole(SILENT)
  const [creating, setCreating] = React.useState(false)
  // The role open in the sheet, to edit or (without roles:update) to view.
  const [open, setOpen] = React.useState<Role>()
  const [deleting, setDeleting] = React.useState<Role>()
  const canCreate = can("roles:create")
  const canUpdate = can("roles:update")
  const canDelete = can("roles:delete")
  const handlers = React.useMemo(
    () => ({
      onEdit: canUpdate ? setOpen : undefined,
      onDelete: canDelete ? setDeleting : undefined,
    }),
    [canUpdate, canDelete]
  )

  useShellPage({ header: { title: "Roles", icon: UserShield01Icon } })

  // "Updated by" by email rather than user ID.
  const rows = useActorLabels(list.data)
  return (
    <>
      <ShellHeaderActions>
        {canCreate && (
          <PrimaryAction onClick={() => setCreating(true)}>Role</PrimaryAction>
        )}
      </ShellHeaderActions>
      {list.error && !list.data ? (
        <ErrorCallout
          title="Couldn't load the roles"
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
      ) : (
        <RowActionsContext.Provider value={handlers}>
          <DataTable
            className="rounded-none border-0"
            description="Each role grants a set of permissions. It's assigned in one kind of scope: an organization, or the site, where it applies in every organization."
            columns={COLUMNS}
            data={rows}
            isLoading={list.isPending}
            getRowId={(role) => role.key}
            onRowClick={(row) => setOpen(row.original)}
            features={ADMIN_TABLE_FEATURES}
            initialState={INITIAL_STATE}
            stateKey="admin-roles"
            exportFileName="roles"
            labels={{ searchPlaceholder: "Search roles…" }}
            emptyState={
              rows.length === 0 ? (
                <EmptyRows
                  message="No roles yet."
                  createLabel="New role"
                  onCreate={canCreate ? () => setCreating(true) : undefined}
                />
              ) : undefined
            }
          />
        </RowActionsContext.Provider>
      )}
      <RoleSheet
        open={creating}
        onOpenChange={setCreating}
        onCreate={(input) => create.mutateAsync(input)}
      />
      <RoleSheet
        open={open !== undefined}
        onOpenChange={(next) => !next && setOpen(undefined)}
        role={open}
        readOnly={!canUpdate}
        onUpdate={(key, data) => update.mutateAsync({ key, data })}
      />
      <DeleteDialog
        open={deleting !== undefined}
        onClose={() => setDeleting(undefined)}
        title={`Delete ${deleting?.name ?? "role"}?`}
        description={
          deleting?.member_count
            ? `It's assigned ${deleting.member_count === 1 ? "once" : `${deleting.member_count} times`}; deleting it revokes every assignment. It can't be undone.`
            : "Nobody holds it. It can't be undone."
        }
        confirmLabel="Delete role"
        onConfirm={() => remove.mutateAsync(deleting!.key)}
      />
    </>
  )
}
