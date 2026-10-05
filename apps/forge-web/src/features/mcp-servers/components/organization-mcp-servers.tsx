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
import { Icon } from "@/components/forge/icon"
import { ShellHeaderActions } from "@/components/forge/shell/index"
import { Chip } from "@/components/forge/status"
import { Button } from "@/components/ui/button"
import { DropdownMenuItem } from "@/components/ui/dropdown-menu"
import { toast } from "@/components/ui/toast"
import { DeleteDialog } from "@/features/admin/components/delete-dialog"
import {
  ADMIN_TABLE_FEATURES,
  PIN_ACTIONS,
} from "@/features/admin/components/table-config"
import { RowMenu } from "@/features/admin/components/table-parts"
import {
  organizationMcpServers,
  startMcpOAuth,
  useAuthMethods,
  useCheckMcpServer,
  useDisconnectMcpServer,
  useOrganizationMcpServers,
  type McpServerCreate,
  type McpServerRecord,
} from "@/features/mcp-servers/lib/api"
import {
  MCP_SERVERS_ICON,
  OAUTH_RETURN_KEY,
  plural,
  STATUS_DISPLAY,
  statusOf,
  type OAuthReturn,
} from "@/features/mcp-servers/lib/display"
import { formatRelative } from "@/lib/format"
import { useScopeAccess } from "@/lib/hierarchy"
import { McpServerDialog } from "./mcp-server-dialog"

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
  open: (server: McpServerRecord) => void
  check?: (server: McpServerRecord) => void
  remove?: (server: McpServerRecord) => void
  /** Auth methods' names, by kind. */
  methods: Record<string, string>
}
const ActionsContext = React.createContext<RowActions | undefined>(undefined)

const helper = createColumnHelper<McpServerRecord>()
const COLUMNS = helper.columns([
  helper.accessor("name", {
    header: "MCP server",
    size: 320,
    enableHiding: false,
    meta: { label: "MCP server" },
    cell: ({ row: { original: server } }) => (
      <span className="flex min-w-0 flex-col">
        <span className="truncate text-[0.8125rem] font-medium text-foreground">
          {server.name}
        </span>
        <span className="truncate font-mono text-2xs text-muted-foreground">
          {server.url}
        </span>
      </span>
    ),
  }),
  helper.accessor((server) => server.auth.kind, {
    id: "auth",
    header: "Authentication",
    size: 180,
    meta: { label: "Authentication", cellClassName: "text-muted-foreground" },
    cell: ({ getValue }) => <AuthLabel kind={getValue()} />,
  }),
  helper.accessor((server) => statusOf(server), {
    id: "status",
    header: "Status",
    size: 150,
    meta: { label: "Status" },
    cell: ({ getValue }) => {
      const display = STATUS_DISPLAY[getValue()]
      return <Chip tone={display.tone}>{display.label}</Chip>
    },
  }),
  helper.accessor((server) => server.tools.length, {
    id: "tools",
    header: "Tools",
    size: 110,
    meta: { label: "Tools", align: "right" },
    cell: ({ row: { original: server } }) => (
      <span className="tabular-nums">
        {server.checked_at ? plural(server.tools.length, "tool") : "—"}
      </span>
    ),
  }),
  helper.accessor((server) => server.checked_at ?? "", {
    id: "checked_at",
    header: "Checked",
    size: 140,
    meta: { label: "Checked", cellClassName: "text-muted-foreground" },
    cell: ({ getValue }) => (getValue() ? formatRelative(getValue()) : "Never"),
  }),
  helper.display({
    id: "actions",
    header: () => <span className="sr-only">Actions</span>,
    size: 64,
    enableSorting: false,
    enableHiding: false,
    enableResizing: false,
    meta: { label: "Actions", align: "right" },
    cell: ({ row }) => <ServerMenu server={row.original} />,
  }),
])

function AuthLabel({ kind }: { kind: string }) {
  const actions = React.useContext(ActionsContext)
  return <>{actions?.methods[kind] ?? kind}</>
}

function ServerMenu({ server }: { server: McpServerRecord }) {
  const actions = React.useContext(ActionsContext)
  if (!actions) return null
  return (
    <RowMenu
      label={server.name}
      onEdit={() => actions.open(server)}
      editLabel={actions.remove ? "Edit…" : "View…"}
      editIcon={MCP_SERVERS_ICON}
      onDelete={actions.remove && (() => actions.remove?.(server))}
    >
      {actions.check && (
        <DropdownMenuItem onClick={() => actions.check?.(server)}>
          <Icon icon="refresh" />
          Check connection
        </DropdownMenuItem>
      )}
    </RowMenu>
  )
}

/**
 * An organization's MCP servers page: the remote MCP servers its agents can
 * use as tools, each with how Forge authenticates to it, and whether it
 * answered when last checked. A server opens in the settings dialog the
 * builders' steps use, to edit, check, or sign in to (OAuth).
 */
export function OrganizationMcpServers({
  organizationId,
  organizationName,
  openServer,
  onOpenServer,
}: {
  organizationId: string
  organizationName: string
  /** The server open in the settings dialog, by ID (from the URL). */
  openServer?: string
  onOpenServer: (id: string | undefined) => void
}) {
  const list = useOrganizationMcpServers(organizationId)
  const methods = useAuthMethods()
  const can = useScopeAccess(`org:${organizationId}`)
  const canManage = can("mcp_servers:manage")
  const scoped = organizationMcpServers.scope({ organizationId })
  const create = scoped.useCreate(SILENT)
  const update = scoped.useUpdate(SILENT)
  const remove = scoped.useDelete(SILENT)
  const check = useCheckMcpServer(organizationId)
  const disconnect = useDisconnectMcpServer(organizationId)
  const [creating, setCreating] = React.useState(false)
  const [removing, setRemoving] = React.useState<McpServerRecord>()
  const servers = React.useMemo(() => list.data ?? [], [list.data])
  const open = servers.find((s) => s.id === openServer)

  const checkAsync = check.mutateAsync
  const runCheck = React.useCallback(
    async (server: McpServerRecord) => {
      const checked = await checkAsync(server.id)
      const status = statusOf(checked)
      if (status === "ok")
        toast.add({
          title: `${checked.name} answered`,
          description: `It has ${plural(checked.tools.length, "tool")}.`,
          type: "success",
        })
      else if (status === "error")
        toast.add({
          title: `Couldn't connect to ${checked.name}`,
          description: checked.last_error ?? undefined,
          type: "error",
        })
    },
    [checkAsync]
  )

  const actions = React.useMemo<RowActions>(
    () => ({
      open: (server) => onOpenServer(server.id),
      methods: Object.fromEntries(
        (methods.data ?? []).map((m) => [m.kind, m.label])
      ),
      ...(canManage
        ? {
            check: (server: McpServerRecord) => void runCheck(server),
            remove: setRemoving,
          }
        : {}),
    }),
    [canManage, methods.data, onOpenServer, runCheck]
  )

  // A server just added or saved is checked, unless someone must sign in first.
  const added = async (input: McpServerCreate) => {
    const made = await create.mutateAsync(input)
    setCreating(false)
    onOpenServer(made.id)
    if (!made.auth.interactive) void runCheck(made)
  }
  const saved = async (id: string, input: McpServerCreate) => {
    const made = await update.mutateAsync({ id, data: input })
    if (made.status === "unchecked" && !made.auth.interactive)
      void runCheck(made)
  }
  const connect = async (server: McpServerRecord) => {
    const { authorization_url } = await startMcpOAuth(organizationId, server.id)
    const back: OAuthReturn = { organizationId, serverId: server.id }
    try {
      sessionStorage.setItem(OAUTH_RETURN_KEY, JSON.stringify(back))
    } catch {
      // Without storage the callback finds the server from the API's answer.
    }
    window.location.assign(authorization_url)
  }

  return (
    <>
      {canManage && (
        <ShellHeaderActions>
          <PrimaryAction onClick={() => setCreating(true)}>
            MCP server
          </PrimaryAction>
        </ShellHeaderActions>
      )}

      {list.error ? (
        <ErrorCallout
          title="Couldn't load the organization's MCP servers"
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
      ) : !list.isPending && servers.length === 0 ? (
        <EmptyWorkspace
          illustration={<EmptyIllustration name="waiting" />}
          title={`Connect ${organizationName}'s MCP servers`}
          description="An MCP server offers tools over the Model Context Protocol. Add a remote one (streamable HTTP) with how Forge signs in to it — an API key, a token or OAuth — and attach its tools to the organization's agents."
          actions={
            canManage ? (
              <Button variant="outline" onClick={() => setCreating(true)}>
                Add an MCP server
              </Button>
            ) : undefined
          }
          steps={[
            { icon: MCP_SERVERS_ICON, label: "Add the server" },
            { icon: "check", label: "Sign in and check it" },
            { icon: "robot", label: "Attach it to agents" },
          ]}
        />
      ) : (
        <ActionsContext.Provider value={actions}>
          <DataTable
            className="rounded-none border-0"
            title="MCP servers"
            description={`The remote MCP servers ${organizationName}'s agents can use as tools. Their credentials are encrypted and never shown again.`}
            columns={COLUMNS}
            data={servers}
            isLoading={list.isPending}
            getRowId={(server) => server.id}
            features={FEATURES}
            initialState={INITIAL_STATE}
            stateKey="organization-mcp-servers"
            exportFileName="mcp-servers"
            labels={{ rows: "MCP servers", rowSingular: "MCP server" }}
            onRowClick={(row) => onOpenServer(row.original.id)}
          />
        </ActionsContext.Provider>
      )}

      <McpServerDialog
        open={creating || open !== undefined}
        onOpenChange={(next) => {
          if (next) return
          setCreating(false)
          onOpenServer(undefined)
        }}
        server={creating ? undefined : open}
        readOnly={!canManage}
        onCreate={added}
        onUpdate={saved}
        onCheck={(server) => void runCheck(server)}
        checking={check.isPending}
        onConnect={connect}
        onDisconnect={(server) => disconnect.mutate(server.id)}
        onDelete={canManage ? setRemoving : undefined}
      />

      <DeleteDialog
        open={removing !== undefined}
        onClose={() => setRemoving(undefined)}
        title={`Delete ${removing?.name ?? "MCP server"}?`}
        description="Its credentials and sign-in are deleted with it, and agents that use it lose its tools. It can't be undone."
        confirmLabel="Delete MCP server"
        onConfirm={async () => {
          await remove.mutateAsync(removing!.id)
          if (openServer === removing?.id) onOpenServer(undefined)
        }}
      />
    </>
  )
}
