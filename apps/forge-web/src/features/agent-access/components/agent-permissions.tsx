import * as React from "react"

import { DeleteDialog } from "@/features/admin/components/delete-dialog"
import {
  PermissionDialog,
  type PermissionScheme,
} from "@/features/admin/components/permission-dialog"
import {
  ADMIN_TABLE_FEATURES,
  DATE_TIME,
  PIN_ACTIONS,
} from "@/features/admin/components/table-config"
import { EmptyRows, RowMenu } from "@/features/admin/components/table-parts"
import { organizationApiKeys } from "@/features/api-keys/lib/api"
import { PrimaryAction } from "@/components/forge/app-shell"
import {
  createColumnHelper,
  DataTable,
  type InitialTableState,
} from "@/components/forge/data-table"
import { ErrorCallout } from "@/components/forge/feedback"
import { ShellHeaderActions } from "@/components/forge/shell"
import { Button } from "@/components/ui/button"
import { useScopeAccess } from "@/lib/hierarchy"
import { useActorLabels } from "@/lib/users"
import {
  agentPermissions,
  useAgentGroups,
  type AgentPermission,
} from "../lib/api"

// The dialogs show failures themselves, so skip the error toast.
const SILENT = { meta: { silent: true } }

const INITIAL_STATE: InitialTableState = {
  columnVisibility: { updated_by: false },
  columnPinning: PIN_ACTIONS,
}

type RowActionHandlers = {
  onEdit?: (permission: AgentPermission) => void
  onDelete?: (permission: AgentPermission) => void
}

// The handlers reach the cells through context, so the columns stay the same
// from render to render.
const RowActionsContext = React.createContext<RowActionHandlers>({})

const helper = createColumnHelper<AgentPermission>()
const COLUMNS = helper.columns([
  helper.accessor("key", {
    header: "Key",
    size: 220,
    meta: { label: "Key", cellClassName: "font-mono text-foreground" },
  }),
  helper.accessor("description", {
    header: "Description",
    size: 260,
    meta: { label: "Description", cellClassName: "text-muted-foreground" },
    cell: ({ getValue }) =>
      getValue() || <span className="text-subtle">—</span>,
  }),
  helper.accessor((permission) => permission.groups.join(", "), {
    id: "groups",
    header: "Company groups",
    size: 260,
    meta: { label: "Company groups", cellClassName: "text-muted-foreground" },
    cell: ({ getValue }) =>
      getValue() || <span className="text-subtle">None</span>,
  }),
  helper.accessor(
    (permission) => permission.api_keys.map((key) => key.name).join(", "),
    {
      id: "api_keys",
      header: "API keys",
      size: 200,
      meta: { label: "API keys", cellClassName: "text-muted-foreground" },
      cell: ({ getValue }) =>
        getValue() || <span className="text-subtle">None</span>,
    }
  ),
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

function RowActions({ permission }: { permission: AgentPermission }) {
  const { onEdit, onDelete } = React.useContext(RowActionsContext)
  return (
    <RowMenu
      label={permission.key}
      onEdit={onEdit && (() => onEdit(permission))}
      onDelete={onDelete && (() => onDelete(permission))}
    />
  )
}

/**
 * An organization's agent permissions, `resource:action` as it names them,
 * each linked to company groups (whoever's sign-in names one holds it) and
 * to its own API keys, in the same dialog as the site's permissions. Its
 * managers (`agent_access:manage`) create, edit and delete them; API keys
 * are offered to those who may see them (`api_keys:manage`).
 */
export function AgentPermissions({
  organizationId,
  organizationName,
  canManage,
}: {
  organizationId: string
  organizationName: string
  canManage: boolean
}) {
  const resource = agentPermissions.scope({ organizationId })
  const list = resource.useList()
  const groups = useAgentGroups(organizationId)
  const can = useScopeAccess(`org:${organizationId}`)
  const keys = organizationApiKeys
    .scope({ organizationId })
    .useList(undefined, { enabled: canManage && can("api_keys:manage") })
  const create = resource.useCreate(SILENT)
  const update = resource.useUpdate(SILENT)
  const remove = resource.useDelete(SILENT)
  const [creating, setCreating] = React.useState(false)
  const [editing, setEditing] = React.useState<AgentPermission>()
  const [deleting, setDeleting] = React.useState<AgentPermission>()
  const handlers = React.useMemo(
    () => (canManage ? { onEdit: setEditing, onDelete: setDeleting } : {}),
    [canManage]
  )
  const scheme = React.useCallback(
    (permission?: AgentPermission): PermissionScheme => {
      // The keys to pick from: the organization's, and those linked already
      // (which someone who can't see the keys still keeps).
      const offered = new Map<string, string>(
        (permission?.api_keys ?? []).map((key) => [key.id, key.name] as const)
      )
      for (const key of keys.data ?? []) offered.set(key.id, key.name)
      return {
        where: organizationName,
        knownGroups: (groups.data ?? []).map((link) => link.group_name),
        example: "invoices:approve",
        apiKeys: [...offered].map(([id, name]) => ({ id, name })),
      }
    },
    [organizationName, groups.data, keys.data]
  )

  // "Updated by" by email rather than user ID.
  const rows = useActorLabels(list.data)
  return (
    <>
      {canManage && (
        <ShellHeaderActions>
          <PrimaryAction onClick={() => setCreating(true)}>
            Agent permission
          </PrimaryAction>
        </ShellHeaderActions>
      )}
      {list.error && !list.data ? (
        <ErrorCallout
          title="Couldn't load the agent permissions"
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
            description={`What ${organizationName}'s agents and workflows can ask for, as resource:action. Whoever's sign-in names a linked company group holds it, and so do linked API keys; each agent and workflow says which it asks for.`}
            columns={COLUMNS}
            data={rows}
            isLoading={list.isPending}
            getRowId={(permission) => String(permission.id)}
            onRowClick={
              canManage ? (row) => setEditing(row.original) : undefined
            }
            features={ADMIN_TABLE_FEATURES}
            initialState={INITIAL_STATE}
            stateKey="agent-permissions"
            exportFileName="agent-permissions"
            labels={{ searchPlaceholder: "Search agent permissions…" }}
            emptyState={
              rows.length === 0 ? (
                <EmptyRows
                  message="No agent permissions yet."
                  createLabel="New agent permission"
                  onCreate={canManage ? () => setCreating(true) : undefined}
                />
              ) : undefined
            }
          />
        </RowActionsContext.Provider>
      )}
      <PermissionDialog
        open={creating}
        onOpenChange={setCreating}
        scheme={scheme()}
        onSubmit={(input) => create.mutateAsync(input)}
      />
      <PermissionDialog
        open={editing !== undefined}
        onOpenChange={(open) => !open && setEditing(undefined)}
        permission={editing}
        scheme={scheme(editing)}
        onSubmit={(input, permission) =>
          update.mutateAsync({ id: permission!.id, data: input })
        }
      />
      <DeleteDialog
        open={deleting !== undefined}
        onClose={() => setDeleting(undefined)}
        title={`Delete ${deleting?.key ?? "agent permission"}?`}
        description={
          (deleting?.groups.length
            ? `Linked groups lose it (${deleting.groups.join(", ")}). `
            : "") +
          (deleting?.api_keys.length
            ? `Linked API keys lose it (${deleting.api_keys.map((key) => key.name).join(", ")}). `
            : "") +
          "Agents and workflows that ask for it are left to those who build them. It can't be undone."
        }
        confirmLabel="Delete agent permission"
        onConfirm={() => remove.mutateAsync(deleting!.id)}
      />
    </>
  )
}
