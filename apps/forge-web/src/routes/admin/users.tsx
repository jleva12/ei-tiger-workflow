import * as React from "react"
import { createFileRoute } from "@tanstack/react-router"

import { DeleteUserDialog, UserDialog } from "@/components/admin/user-dialogs"
import {
  ADMIN_TABLE_FEATURES,
  DATE_TIME,
  PIN_ACTIONS,
} from "@/components/admin/table-config"
import { EmptyRows, RowMenu } from "@/components/admin/table-parts"
import { UserAvatar } from "@/components/admin/user-picker"
import { PrimaryAction } from "@/components/forge/app-shell"
import {
  createColumnHelper,
  DataTable,
  type InitialTableState,
} from "@/components/forge/data-table"
import { ErrorCallout } from "@/components/forge/feedback"
import { ShellHeaderActions, useShellPage } from "@/components/forge/shell"
import { Button } from "@/components/ui/button"
import { useScopeAccess } from "@/lib/hierarchy"
import { useActorLabels, userName, users, type User } from "@/lib/users"

export const Route = createFileRoute("/admin/users")({
  // Site administration: the /admin layout lets only site administrators in.
  component: UsersPage,
})

// The dialogs show failures themselves, so skip the error toast.
const SILENT = { meta: { silent: true } }

const INITIAL_STATE: InitialTableState = {
  columnVisibility: { created_at: false, created_by: false },
  columnPinning: PIN_ACTIONS,
}

type RowActionHandlers = {
  onEdit?: (user: User) => void
  onDelete?: (user: User) => void
}

// The handlers reach the cells through context, so the columns stay the same
// from render to render.
const RowActionsContext = React.createContext<RowActionHandlers>({})

const helper = createColumnHelper<User>()
const COLUMNS = helper.columns([
  helper.accessor(userName, {
    id: "name",
    header: "Name",
    size: 240,
    meta: { label: "Name" },
    cell: ({ row }) => (
      <span className="flex min-w-0 items-center gap-2 font-medium text-foreground">
        <UserAvatar user={row.original} />
        <span className="truncate">{userName(row.original)}</span>
      </span>
    ),
  }),
  helper.accessor("email", {
    header: "Email",
    size: 260,
    meta: { label: "Email", cellClassName: "text-muted-foreground" },
  }),
  helper.accessor("msid", {
    header: "MS ID",
    size: 140,
    meta: { label: "MS ID", cellClassName: "font-mono text-muted-foreground" },
  }),
  helper.accessor("updated_at", {
    header: "Updated",
    size: 170,
    meta: { label: "Updated", format: "datetime", formatOptions: DATE_TIME },
  }),
  helper.accessor("created_at", {
    header: "Added",
    size: 170,
    meta: { label: "Added", format: "datetime", formatOptions: DATE_TIME },
  }),
  helper.accessor("created_by", {
    header: "Added by",
    size: 160,
    meta: { label: "Added by", cellClassName: "text-muted-foreground" },
  }),
  helper.display({
    id: "actions",
    header: () => <span className="sr-only">Actions</span>,
    size: 64,
    enableSorting: false,
    enableHiding: false,
    enableResizing: false,
    meta: { label: "Actions", align: "right" },
    cell: ({ row }) => <RowActions user={row.original} />,
  }),
])

function RowActions({ user }: { user: User }) {
  const { onEdit, onDelete } = React.useContext(RowActionsContext)
  return (
    <RowMenu
      label={userName(user)}
      onEdit={onEdit && (() => onEdit(user))}
      onDelete={onDelete && (() => onDelete(user))}
      deleteLabel="Remove…"
    />
  )
}

function UsersPage() {
  const can = useScopeAccess("site")
  const list = users.useList()
  const create = users.useCreate(SILENT)
  const update = users.useUpdate(SILENT)
  const remove = users.useDelete(SILENT)
  const [creating, setCreating] = React.useState(false)
  const [editing, setEditing] = React.useState<User>()
  const [deleting, setDeleting] = React.useState<User>()
  const canCreate = can("users:create")
  const canUpdate = can("users:update")
  const canDelete = can("users:delete")
  const handlers = React.useMemo(
    () => ({
      onEdit: canUpdate ? setEditing : undefined,
      onDelete: canDelete ? setDeleting : undefined,
    }),
    [canUpdate, canDelete]
  )

  useShellPage({ header: { title: "Users", icon: "team" } })

  // "Added by" by email rather than user ID.
  const rows = useActorLabels(list.data)
  return (
    <>
      <ShellHeaderActions>
        {canCreate && (
          <PrimaryAction onClick={() => setCreating(true)}>User</PrimaryAction>
        )}
      </ShellHeaderActions>
      {list.error && !list.data ? (
        <ErrorCallout
          title="Couldn't load the users"
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
            description="Everyone who uses Forge. Pick from them when giving people roles, such as an organization's administrators."
            columns={COLUMNS}
            data={rows}
            isLoading={list.isPending}
            getRowId={(user) => user.id}
            features={ADMIN_TABLE_FEATURES}
            initialState={INITIAL_STATE}
            stateKey="admin-users"
            exportFileName="users"
            labels={{ searchPlaceholder: "Search users…" }}
            // With rows, an empty table means the search matched nothing.
            emptyState={
              rows.length === 0 ? (
                <EmptyRows
                  message="No users yet."
                  createLabel="New user"
                  onCreate={canCreate ? () => setCreating(true) : undefined}
                />
              ) : undefined
            }
          />
        </RowActionsContext.Provider>
      )}
      <UserDialog
        open={creating}
        onOpenChange={setCreating}
        onSubmit={(input) => create.mutateAsync(input)}
      />
      <UserDialog
        open={editing !== undefined}
        onOpenChange={(open) => !open && setEditing(undefined)}
        user={editing}
        onSubmit={(input, user) =>
          update.mutateAsync({ id: user!.id, data: input })
        }
      />
      <DeleteUserDialog
        user={deleting}
        onClose={() => setDeleting(undefined)}
        onConfirm={(user) => remove.mutateAsync(user.id)}
      />
    </>
  )
}
