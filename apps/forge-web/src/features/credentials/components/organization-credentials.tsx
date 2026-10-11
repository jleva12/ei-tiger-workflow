import * as React from "react"

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
import {
  organizationCredentials,
  useCredentialAuthMethods,
  useDisconnectCredential,
  useOrganizationCredentials,
  type CredentialCreate,
  type CredentialRecord,
} from "@/features/credentials/lib/api"
import {
  CREDENTIALS_ICON,
  sentToLabel,
} from "@/features/credentials/lib/display"
import { formatRelative } from "@/lib/format"
import { useScopeAccess } from "@/lib/hierarchy"
import { CredentialDialog } from "./credential-dialog"

const FEATURES: Partial<DataTableFeatureConfig> = {
  ...ADMIN_TABLE_FEATURES,
  pagination: false,
  faceting: false,
}

const INITIAL_STATE: InitialTableState = { columnPinning: PIN_ACTIONS }

// The settings and delete dialogs show failures themselves.
const SILENT = { meta: { silent: true } }

// Row actions reach the cells through context, so the columns stay put.
type RowActions = {
  open: (credential: CredentialRecord) => void
  remove?: (credential: CredentialRecord) => void
  /** Auth methods' names, by kind. */
  methods: Record<string, string>
}
const ActionsContext = React.createContext<RowActions | undefined>(undefined)

const helper = createColumnHelper<CredentialRecord>()
const COLUMNS = helper.columns([
  helper.accessor("name", {
    header: "Credential",
    size: 300,
    enableHiding: false,
    meta: { label: "Credential" },
    cell: ({ row: { original: credential } }) => (
      <span className="flex min-w-0 flex-col">
        <span className="truncate text-[0.8125rem] font-medium text-foreground">
          {credential.name}
        </span>
        {credential.description && (
          <span className="truncate text-2xs text-muted-foreground">
            {credential.description}
          </span>
        )}
      </span>
    ),
  }),
  helper.accessor((credential) => credential.auth.kind, {
    id: "auth",
    header: "Signs in with",
    size: 200,
    meta: { label: "Signs in with", cellClassName: "text-muted-foreground" },
    cell: ({ getValue }) => <MethodLabel kind={getValue()} />,
  }),
  helper.accessor((credential) => sentToLabel(credential.urls), {
    id: "urls",
    header: "Sent only to",
    size: 260,
    meta: { label: "Sent only to" },
    cell: ({ getValue }) => (
      <span className="truncate font-mono text-2xs text-muted-foreground">
        {getValue()}
      </span>
    ),
  }),
  helper.accessor(
    (credential) =>
      !credential.auth.interactive
        ? "ready"
        : credential.auth.connected
          ? "signed-in"
          : "needs-sign-in",
    {
      id: "status",
      header: "Status",
      size: 140,
      meta: { label: "Status" },
      cell: ({ getValue }) =>
        getValue() === "needs-sign-in" ? (
          <Chip tone="warning">Needs sign-in</Chip>
        ) : getValue() === "signed-in" ? (
          <Chip tone="success">Signed in</Chip>
        ) : (
          <Chip tone="neutral">Ready</Chip>
        ),
    }
  ),
  helper.accessor("updated_at", {
    header: "Changed",
    size: 140,
    meta: { label: "Changed", cellClassName: "text-muted-foreground" },
    cell: ({ getValue }) => formatRelative(getValue()),
  }),
  helper.display({
    id: "actions",
    header: () => <span className="sr-only">Actions</span>,
    size: 64,
    enableSorting: false,
    enableHiding: false,
    enableResizing: false,
    meta: { label: "Actions", align: "right" },
    cell: ({ row }) => <CredentialMenu credential={row.original} />,
  }),
])

function MethodLabel({ kind }: { kind: string }) {
  const actions = React.useContext(ActionsContext)
  return <>{actions?.methods[kind] ?? kind}</>
}

function CredentialMenu({ credential }: { credential: CredentialRecord }) {
  const actions = React.useContext(ActionsContext)
  if (!actions) return null
  return (
    <RowMenu
      label={credential.name}
      onEdit={() => actions.open(credential)}
      editLabel={actions.remove ? "Edit…" : "View…"}
      editIcon={CREDENTIALS_ICON}
      onDelete={actions.remove && (() => actions.remove?.(credential))}
    />
  )
}

/**
 * An organization's Credentials page: how its workflows' HTTP steps and its
 * agents' HTTP and OpenAPI tools sign in to its APIs, each sent only to the
 * addresses it names. A credential opens in the settings dialog the
 * builders' steps use, to edit or sign in with (OAuth).
 */
export function OrganizationCredentials({
  organizationId,
  organizationName,
  openCredential,
  onOpenCredential,
}: {
  organizationId: string
  organizationName: string
  /** The credential open in the settings dialog, by ID (from the URL). */
  openCredential?: string
  onOpenCredential: (id: string | undefined) => void
}) {
  const list = useOrganizationCredentials(organizationId)
  const methods = useCredentialAuthMethods()
  const can = useScopeAccess(`org:${organizationId}`)
  const canManage = can("credentials:manage")
  const scoped = organizationCredentials.scope({ organizationId })
  const create = scoped.useCreate(SILENT)
  const update = scoped.useUpdate(SILENT)
  const remove = scoped.useDelete(SILENT)
  const disconnect = useDisconnectCredential(organizationId)
  const [creating, setCreating] = React.useState(false)
  const [removing, setRemoving] = React.useState<CredentialRecord>()
  const credentials = React.useMemo(() => list.data ?? [], [list.data])
  const open = credentials.find((c) => c.id === openCredential)

  const actions = React.useMemo<RowActions>(
    () => ({
      open: (credential) => onOpenCredential(credential.id),
      methods: Object.fromEntries(
        (methods.data ?? []).map((m) => [m.kind, m.label])
      ),
      ...(canManage ? { remove: setRemoving } : {}),
    }),
    [canManage, methods.data, onOpenCredential]
  )

  const added = async (input: CredentialCreate) => {
    const made = await create.mutateAsync(input)
    setCreating(false)
    onOpenCredential(made.id)
  }
  const saved = async (id: string, input: CredentialCreate) => {
    await update.mutateAsync({ id, data: input })
  }

  return (
    <>
      {canManage && (
        <ShellHeaderActions>
          <PrimaryAction onClick={() => setCreating(true)}>
            Credential
          </PrimaryAction>
        </ShellHeaderActions>
      )}

      {list.error ? (
        <ErrorCallout
          title="Couldn't load the organization's credentials"
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
      ) : !list.isPending && credentials.length === 0 ? (
        <EmptyWorkspace
          illustration={<EmptyIllustration name="waiting" />}
          title={`Sign ${organizationName}'s workflows and agents in to its APIs`}
          description="A credential is how an HTTP step, HTTP tool or OpenAPI tool signs in to an API: an API key in a header or the query, a bearer token, basic auth, or OAuth. It's sent only to the addresses it names, and its secrets are encrypted and never shown again."
          actions={
            canManage ? (
              <Button variant="outline" onClick={() => setCreating(true)}>
                Add a credential
              </Button>
            ) : undefined
          }
          steps={[
            { icon: CREDENTIALS_ICON, label: "Add the credential" },
            { icon: "check", label: "Say where it may go" },
            { icon: "robot", label: "Pick it on a step or tool" },
          ]}
        />
      ) : (
        <ActionsContext.Provider value={actions}>
          <DataTable
            className="rounded-none border-0"
            title="Credentials"
            description={`How ${organizationName}'s HTTP steps and HTTP and OpenAPI tools sign in to its APIs. Secrets are encrypted and never shown again.`}
            columns={COLUMNS}
            data={credentials}
            isLoading={list.isPending}
            getRowId={(credential) => credential.id}
            features={FEATURES}
            initialState={INITIAL_STATE}
            stateKey="organization-credentials"
            exportFileName="credentials"
            labels={{ rows: "credentials", rowSingular: "credential" }}
            onRowClick={(row) => onOpenCredential(row.original.id)}
          />
        </ActionsContext.Provider>
      )}

      <CredentialDialog
        open={creating || open !== undefined}
        onOpenChange={(next) => {
          if (next) return
          setCreating(false)
          onOpenCredential(undefined)
        }}
        credential={creating ? undefined : open}
        readOnly={!canManage}
        onCreate={added}
        onUpdate={saved}
        onDisconnect={(credential) => disconnect.mutate(credential.id)}
        onDelete={canManage ? setRemoving : undefined}
      />

      <DeleteDialog
        open={removing !== undefined}
        onClose={() => setRemoving(undefined)}
        title={`Delete ${removing?.name ?? "credential"}?`}
        description="Its secrets and sign-in are deleted with it. Steps and tools that use it fail until they're given another. It can't be undone."
        confirmLabel="Delete credential"
        onConfirm={async () => {
          await remove.mutateAsync(removing!.id)
          if (openCredential === removing?.id) onOpenCredential(undefined)
        }}
      />
    </>
  )
}
