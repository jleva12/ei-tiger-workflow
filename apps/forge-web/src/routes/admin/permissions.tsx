import * as React from "react"
import { Key01Icon } from "@hugeicons/core-free-icons"
import { createFileRoute } from "@tanstack/react-router"

import { DeleteDialog } from "@/features/admin/components/delete-dialog"
import { PermissionDialog } from "@/features/admin/components/permission-dialog"
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
import { permissions, useRoles, type Permission } from "@/lib/access"
import { useScopeAccess } from "@/lib/hierarchy"
import { useActorLabels } from "@/lib/users"

export const Route = createFileRoute("/admin/permissions")({
  // Site administration: the /admin layout lets only site administrators in.
  component: PermissionsPage,
})

// The dialogs show failures themselves, so skip the error toast.
const SILENT = { meta: { silent: true } }

/** A permission and the names of the roles that grant it. */
type PermissionRow = Permission & { roles: string[] }

const INITIAL_STATE: InitialTableState = {
  columnVisibility: { updated_by: false },
  columnPinning: PIN_ACTIONS,
}

type RowActionHandlers = {
  onEdit?: (permission: PermissionRow) => void
  onDelete?: (permission: PermissionRow) => void
}

// The handlers reach the cells through context, so the columns stay the same
// from render to render.
const RowActionsContext = React.createContext<RowActionHandlers>({})

const helper = createColumnHelper<PermissionRow>()
const COLUMNS = helper.columns([
  helper.accessor("key", {
    header: "Key",
    size: 220,
    meta: { label: "Key", cellClassName: "font-mono text-foreground" },
  }),
  helper.accessor("description", {
    header: "Description",
    size: 280,
    meta: { label: "Description", cellClassName: "text-muted-foreground" },
    cell: ({ getValue }) =>
      getValue() || <span className="text-subtle">—</span>,
  }),
  helper.accessor((permission) => permission.roles.join(", "), {
    id: "roles",
    header: "Granted by",
    size: 280,
    meta: { label: "Granted by", cellClassName: "text-muted-foreground" },
    cell: ({ getValue }) =>
      getValue() || <span className="text-subtle">No roles</span>,
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
    cell: ({ row }) => <RowActions permission={row.original} />,
  }),
])

function RowActions({ permission }: { permission: PermissionRow }) {
  const { onEdit, onDelete } = React.useContext(RowActionsContext)
  return (
    <RowMenu
      label={permission.key}
      onEdit={onEdit && (() => onEdit(permission))}
      onDelete={onDelete && (() => onDelete(permission))}
    />
  )
}

function PermissionsPage() {
  const can = useScopeAccess("site")
  const list = permissions.useList()
  const roles = useRoles()
  const create = permissions.useCreate(SILENT)
  const update = permissions.useUpdate(SILENT)
  const remove = permissions.useDelete(SILENT)
  const [creating, setCreating] = React.useState(false)
  const [editing, setEditing] = React.useState<PermissionRow>()
  const [deleting, setDeleting] = React.useState<PermissionRow>()
  const canCreate = can("permissions:create")
  const canUpdate = can("permissions:update")
  const canDelete = can("permissions:delete")
  const handlers = React.useMemo(
    () => ({
      onEdit: canUpdate ? setEditing : undefined,
      onDelete: canDelete ? setDeleting : undefined,
    }),
    [canUpdate, canDelete]
  )

  // "Updated by" by email rather than user ID.
  const labeled = useActorLabels(list.data)
  const rows = React.useMemo(() => {
    const grantedBy = new Map<number, string[]>()
    for (const role of roles.data ?? []) {
      for (const permission of role.permissions) {
        grantedBy.set(permission.id, [
          ...(grantedBy.get(permission.id) ?? []),
          role.name,
        ])
      }
    }
    return labeled.map((permission) => ({
      ...permission,
      roles: grantedBy.get(permission.id) ?? [],
    }))
  }, [labeled, roles.data])

  useShellPage({ header: { title: "Permissions", icon: Key01Icon } })

  const deletingCount = deleting?.roles.length ?? 0
  return (
    <>
      <ShellHeaderActions>
        {canCreate && (
          <PrimaryAction onClick={() => setCreating(true)}>
            Permission
          </PrimaryAction>
        )}
      </ShellHeaderActions>
      {list.error && !list.data ? (
        <ErrorCallout
          title="Couldn't load the permissions"
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
            description="What roles can let people do, as resource:action. The API checks them on every request."
            columns={COLUMNS}
            data={rows}
            isLoading={list.isPending}
            getRowId={(permission) => String(permission.id)}
            onRowClick={
              canUpdate ? (row) => setEditing(row.original) : undefined
            }
            features={ADMIN_TABLE_FEATURES}
            initialState={INITIAL_STATE}
            stateKey="admin-permissions"
            exportFileName="permissions"
            labels={{ searchPlaceholder: "Search permissions…" }}
            emptyState={
              rows.length === 0 ? (
                <EmptyRows
                  message="No permissions yet."
                  createLabel="New permission"
                  onCreate={canCreate ? () => setCreating(true) : undefined}
                />
              ) : undefined
            }
          />
        </RowActionsContext.Provider>
      )}
      <PermissionDialog
        open={creating}
        onOpenChange={setCreating}
        onSubmit={(input) => create.mutateAsync(input)}
      />
      <PermissionDialog
        open={editing !== undefined}
        onOpenChange={(open) => !open && setEditing(undefined)}
        permission={editing}
        grantedBy={editing?.roles.length}
        onSubmit={(input, permission) =>
          update.mutateAsync({ id: permission!.id, data: input })
        }
      />
      <DeleteDialog
        open={deleting !== undefined}
        onClose={() => setDeleting(undefined)}
        title={`Delete ${deleting?.key ?? "permission"}?`}
        description={
          deletingCount > 0
            ? `${deletingCount === 1 ? "One role grants" : `${deletingCount} roles grant`} it (${deleting?.roles.join(", ")}); they'll stop. It can't be undone.`
            : "No role grants it. It can't be undone."
        }
        confirmLabel="Delete permission"
        onConfirm={() => remove.mutateAsync(deleting!.id)}
      />
    </>
  )
}
