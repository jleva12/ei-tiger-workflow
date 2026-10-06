import * as React from "react"
import { Key01Icon } from "@hugeicons/core-free-icons"

import { PrimaryAction } from "@/components/forge/app-shell"
import {
  createColumnHelper,
  DataTable,
  type DataTableFeatureConfig,
  type InitialTableState,
} from "@/components/forge/data-table/index"
import {
  EmptyIllustration,
  EmptyWorkspace,
} from "@/components/forge/empty-state"
import { ErrorCallout } from "@/components/forge/feedback"
import { ShellHeaderActions } from "@/components/forge/shell/index"
import { Chip } from "@/components/forge/status"
import { Button } from "@/components/ui/button"
import { DeleteDialog } from "@/features/admin/components/delete-dialog"
import {
  ADMIN_TABLE_FEATURES,
  PIN_ACTIONS,
} from "@/features/admin/components/table-config"
import { RowMenu } from "@/features/admin/components/table-parts"
import { formatDate, formatRelative } from "@/lib/format"
import {
  organizationApiKeys,
  useCreateApiKey,
  useOrganizationApiKeys,
  type ApiKeyCreated,
  type ApiKeyRecord,
} from "../lib/api"
import { ApiKeyDialog, type ApiKeyInput } from "./api-key-dialog"
import { ApiKeySecretDialog } from "./api-key-secret-dialog"
import { RuntimeAccess } from "./runtime-access"

const FEATURES: Partial<DataTableFeatureConfig> = {
  ...ADMIN_TABLE_FEATURES,
  pagination: false,
  faceting: false,
}

const INITIAL_STATE: InitialTableState = { columnPinning: PIN_ACTIONS }

// The dialogs show failures themselves.
const SILENT = { meta: { silent: true } }

type RowActions = {
  edit: (key: ApiKeyRecord) => void
  remove: (key: ApiKeyRecord) => void
}
const ActionsContext = React.createContext<RowActions | undefined>(undefined)

const helper = createColumnHelper<ApiKeyRecord>()
const COLUMNS = helper.columns([
  helper.accessor("name", {
    header: "API key",
    size: 280,
    enableHiding: false,
    meta: { label: "API key" },
    cell: ({ row: { original: key } }) => (
      <span className="flex min-w-0 flex-col">
        <span className="truncate text-[0.8125rem] font-medium text-foreground">
          {key.name}
        </span>
        <span className="truncate font-mono text-2xs text-muted-foreground">
          {key.hint}
        </span>
      </span>
    ),
  }),
  helper.accessor((key) => key.role_name ?? "", {
    id: "role",
    header: "Role",
    size: 180,
    meta: { label: "Role" },
    cell: ({ row: { original: key } }) =>
      key.role_name ?? <Chip tone="warning">No role</Chip>,
  }),
  helper.accessor((key) => key.last_used_at ?? "", {
    id: "last_used_at",
    header: "Last used",
    size: 140,
    meta: { label: "Last used", cellClassName: "text-muted-foreground" },
    cell: ({ getValue }) => (getValue() ? formatRelative(getValue()) : "Never"),
  }),
  helper.accessor((key) => key.expires_at ?? "", {
    id: "expires_at",
    header: "Expires",
    size: 140,
    meta: { label: "Expires", cellClassName: "text-muted-foreground" },
    cell: ({ row: { original: key } }) =>
      key.expired ? (
        <Chip tone="danger">Expired</Chip>
      ) : key.expires_at ? (
        formatDate(key.expires_at)
      ) : (
        "Never"
      ),
  }),
  helper.accessor("created_by_name", {
    header: "Made by",
    size: 180,
    meta: { label: "Made by", cellClassName: "text-muted-foreground" },
    cell: ({ row: { original: key } }) => (
      <span className="truncate">
        {key.created_by_name} · {formatRelative(key.created_at)}
      </span>
    ),
  }),
  helper.display({
    id: "actions",
    header: () => <span className="sr-only">Actions</span>,
    size: 64,
    enableSorting: false,
    enableHiding: false,
    enableResizing: false,
    meta: { label: "Actions", align: "right" },
    cell: ({ row }) => <KeyMenu apiKey={row.original} />,
  }),
])

function KeyMenu({ apiKey }: { apiKey: ApiKeyRecord }) {
  const actions = React.useContext(ActionsContext)
  if (!actions) return null
  return (
    <RowMenu
      label={apiKey.name}
      onEdit={() => actions.edit(apiKey)}
      editLabel="Rename or change role…"
      editIcon={Key01Icon}
      onDelete={() => actions.remove(apiKey)}
    />
  )
}

/**
 * An organization's API keys page, for those who manage them: each key's
 * name and last four characters, its role, when it was last used and when
 * it expires. A key's secret shows once, as it's made.
 */
export function OrganizationApiKeys({
  organizationId,
  organizationName,
}: {
  organizationId: string
  organizationName: string
}) {
  const list = useOrganizationApiKeys(organizationId)
  const scoped = organizationApiKeys.scope({ organizationId })
  const create = useCreateApiKey(organizationId)
  const update = scoped.useUpdate(SILENT)
  const remove = scoped.useDelete(SILENT)
  const [creating, setCreating] = React.useState(false)
  const [editing, setEditing] = React.useState<ApiKeyRecord>()
  const [removing, setRemoving] = React.useState<ApiKeyRecord>()
  const [made, setMade] = React.useState<ApiKeyCreated>()
  const keys = React.useMemo(() => list.data ?? [], [list.data])
  const actions = React.useMemo<RowActions>(
    () => ({ edit: setEditing, remove: setRemoving }),
    []
  )

  const submit = async (input: ApiKeyInput) => {
    if (editing) {
      await update.mutateAsync({
        id: editing.id,
        data: { name: input.name, role: input.role },
      })
      return
    }
    setMade(await create.mutateAsync(input))
  }

  return (
    <>
      <ShellHeaderActions>
        <PrimaryAction onClick={() => setCreating(true)}>API key</PrimaryAction>
      </ShellHeaderActions>

      {list.error ? (
        <ErrorCallout
          title="Couldn't load the organization's API keys"
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
      ) : !list.isPending && keys.length === 0 ? (
        <EmptyWorkspace
          illustration={<EmptyIllustration name="waiting" />}
          title={`Let apps call ${organizationName}'s agents and workflows`}
          description="An API key is what an outside app sends instead of a person's sign-in. Each holds one of the organization's roles: API caller runs its agents and workflows, and nothing else."
          actions={
            <Button variant="outline" onClick={() => setCreating(true)}>
              Make an API key
            </Button>
          }
          steps={[
            { icon: Key01Icon, label: "Make a key" },
            { icon: "copy", label: "Copy it once" },
            { icon: "robot", label: "Call agents and workflows" },
          ]}
        />
      ) : (
        <div className="flex flex-col gap-3">
          <div className="px-1">
            <RuntimeAccess organizationId={organizationId} />
          </div>
          <ActionsContext.Provider value={actions}>
            <DataTable
              className="rounded-none border-0"
              title="API keys"
              description={`What outside apps send to call ${organizationName}'s API. Each can do what its role allows here.`}
              columns={COLUMNS}
              data={keys}
              isLoading={list.isPending}
              getRowId={(key) => key.id}
              features={FEATURES}
              initialState={INITIAL_STATE}
              stateKey="organization-api-keys"
              exportFileName="api-keys"
              labels={{ rows: "API keys", rowSingular: "API key" }}
              onRowClick={(row) => setEditing(row.original)}
            />
          </ActionsContext.Provider>
        </div>
      )}

      <ApiKeyDialog
        open={creating || editing !== undefined}
        onOpenChange={(next) => {
          if (next) return
          setCreating(false)
          setEditing(undefined)
        }}
        apiKey={editing}
        organizationId={organizationId}
        organizationName={organizationName}
        onSubmit={submit}
      />

      <ApiKeySecretDialog apiKey={made} onClose={() => setMade(undefined)} />

      <DeleteDialog
        open={removing !== undefined}
        onClose={() => setRemoving(undefined)}
        title={`Delete ${removing?.name ?? "the API key"}?`}
        description="Apps using it are refused from their next call. It can't be undone."
        confirmLabel="Delete API key"
        onConfirm={async () => {
          await remove.mutateAsync(removing!.id)
        }}
      />
    </>
  )
}
