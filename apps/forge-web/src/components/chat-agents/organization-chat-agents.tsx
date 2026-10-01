import * as React from "react"
import { useNavigate } from "@tanstack/react-router"

import {
  ADMIN_TABLE_FEATURES,
  PIN_ACTIONS,
} from "@/components/admin/table-config"
import { RowMenu } from "@/components/admin/table-parts"
import { ImportDialog } from "@/components/builder/import-dialog"
import { downloadJson } from "@/components/builder/utils"
import { PrimaryAction } from "@/components/forge/app-shell"
import {
  createColumnHelper,
  DataTable,
  type DataTableFeatureConfig,
  type InitialTableState,
} from "@/components/forge/data-table"
import {
  EmptyIllustration,
  EmptyWorkspace,
} from "@/components/forge/empty-state"
import { ErrorCallout } from "@/components/forge/feedback"
import { Icon } from "@/components/forge/icon"
import { ShellHeaderActions } from "@/components/forge/shell"
import { Chip } from "@/components/forge/status"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { DropdownMenuItem } from "@/components/ui/dropdown-menu"
import { toast } from "@/components/ui/toast"
import { useOrganizationAgents } from "@/lib/agents/api"
import { toApiError } from "@/lib/api"
import {
  useCreateChatAgent,
  useDeleteChatAgent,
  useOrganizationChatAgents,
  type ChatAgentRecord,
} from "@/lib/chat-agents/api"
import {
  CHAT_AGENT_FORMAT,
  copyChatAgent,
  newChatAgent,
  parseChatAgent,
  storedChatAgent,
  toGraph,
  usesOf,
  type ChatAgentDocument,
} from "@/lib/chat-agents/document"
import { exampleChatAgent } from "@/lib/chat-agents/example"
import { AGENT_LIKE, CHAT_AGENTS_ICON } from "@/lib/chat-agents/model"
import {
  validateChatAgent,
  type ChatAgentValidationContext,
} from "@/lib/chat-agents/validate"
import { formatRelative } from "@/lib/format"
import { useScopeAccess } from "@/lib/hierarchy"
import { parseTimestamp } from "@/lib/timestamps"
import { chatAgentFileName } from "./chat-agent-files"

/** A row of the table: one chat agent. */
type AgentRow = {
  id: string
  name: string
  description: string
  /** What it calls. */
  tools: number
  /** Who it hands off to, or calls: its sub-agents and saved agents. */
  agents: number
  errors: number
  warnings: number
  updated_at: string
  /** Who saved it last. */
  updated_by: string
  doc: ChatAgentDocument
}

const toRow = (
  record: ChatAgentRecord,
  context: ChatAgentValidationContext
): AgentRow => {
  const doc = storedChatAgent(record.document, record.organization_id)
  const issues = validateChatAgent(toGraph(doc), { ...context, selfId: doc.id })
  const errors = issues.filter((i) => i.level === "error").length
  const agents = doc.nodes.filter(
    (n) => n.kind === "sub_agent" || n.kind === "saved_agent"
  ).length
  return {
    id: doc.id,
    name: doc.name,
    description: doc.description,
    tools: doc.nodes.filter(
      (n) => !AGENT_LIKE.has(n.kind) && n.kind !== "saved_agent"
    ).length,
    agents,
    errors,
    warnings: issues.length - errors,
    updated_at: record.updated_at,
    updated_by: record.updated_by_name,
    doc,
  }
}

const FEATURES: Partial<DataTableFeatureConfig> = {
  ...ADMIN_TABLE_FEATURES,
  pagination: false,
  faceting: false,
}

const INITIAL_STATE: InitialTableState = { columnPinning: PIN_ACTIONS }

// Row actions reach the cells through context, so the columns stay put.
// Without agents:manage there's only opening and exporting.
type RowActions = {
  open: (row: AgentRow) => void
  duplicate?: (row: AgentRow) => void
  remove?: (row: AgentRow) => void
}
const ActionsContext = React.createContext<RowActions | undefined>(undefined)

const plural = (n: number, word: string) =>
  `${n} ${n === 1 ? word : `${word}s`}`

const helper = createColumnHelper<AgentRow>()
const COLUMNS = helper.columns([
  helper.accessor("name", {
    header: "Agent",
    size: 320,
    enableHiding: false,
    meta: { label: "Agent" },
    cell: ({ row: { original: row } }) => (
      <span className="flex min-w-0 flex-col">
        <span className="truncate text-[0.8125rem] font-medium text-foreground">
          {row.name}
        </span>
        <span className="truncate text-2xs text-muted-foreground">
          {row.description || "No description"}
        </span>
      </span>
    ),
  }),
  helper.accessor("tools", {
    header: "Attached",
    size: 200,
    meta: { label: "Attached", align: "right" },
    cell: ({ row: { original: row } }) => (
      <span className="tabular-nums">
        {plural(row.tools, "tool")}
        {row.agents > 0 && (
          <span className="text-muted-foreground">{` · ${plural(row.agents, "sub-agent")}`}</span>
        )}
      </span>
    ),
  }),
  helper.accessor((row) => row.errors * 1000 + row.warnings, {
    id: "state",
    header: "State",
    size: 132,
    meta: { label: "State" },
    cell: ({ row: { original: row } }) =>
      row.errors ? (
        <Chip tone="danger">{plural(row.errors, "error")}</Chip>
      ) : row.warnings ? (
        <Chip tone="warning">{plural(row.warnings, "warning")}</Chip>
      ) : (
        <Chip tone="success">Ready</Chip>
      ),
  }),
  helper.accessor("updated_at", {
    header: "Changed",
    size: 180,
    meta: { label: "Changed", cellClassName: "text-muted-foreground" },
    cell: ({ row: { original: row } }) => (
      <span
        className="flex min-w-0 flex-col"
        title={`${parseTimestamp(row.updated_at).toLocaleString()} by ${row.updated_by}`}
      >
        <span className="truncate">{formatRelative(row.updated_at)}</span>
        <span className="truncate text-2xs text-subtle">
          by {row.updated_by}
        </span>
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
    cell: ({ row }) => <AgentMenu row={row.original} />,
  }),
])

function AgentMenu({ row }: { row: AgentRow }) {
  const actions = React.useContext(ActionsContext)
  if (!actions) return null
  return (
    <RowMenu
      label={row.name}
      onEdit={() => actions.open(row)}
      editLabel="Open"
      editIcon={CHAT_AGENTS_ICON}
      onDelete={actions.remove && (() => actions.remove?.(row))}
    >
      {actions.duplicate && (
        <DropdownMenuItem onClick={() => actions.duplicate?.(row)}>
          <Icon icon="copy" />
          Duplicate
        </DropdownMenuItem>
      )}
      <DropdownMenuItem
        onClick={() => downloadJson(row.doc, chatAgentFileName(row.doc))}
      >
        <Icon icon="download" />
        Export JSON
      </DropdownMenuItem>
    </RowMenu>
  )
}

/** "Untitled agent", or "Untitled agent 2" when that's taken. */
function freshName(base: string, taken: ChatAgentDocument[]) {
  const names = new Set(taken.map((a) => a.name))
  if (!names.has(base)) return base
  for (let n = 2; ; n += 1)
    if (!names.has(`${base} ${n}`)) return `${base} ${n}`
}

/**
 * An organization's Agents page: the chat agents it has built (one Google
 * ADK agent each, with its tools and sub-agents), each opening its
 * builder. New agent starts one with just the agent; the example and an
 * import start from more. Kept in this browser until the admin API keeps
 * them.
 */
export function OrganizationChatAgents({
  organizationId,
  organizationName,
}: {
  organizationId: string
  organizationName: string
}) {
  const navigate = useNavigate()
  const list = useOrganizationChatAgents(organizationId)
  const { records, agents } = list
  const { agents: workflows, isSuccess: workflowsKnown } =
    useOrganizationAgents(organizationId)
  const can = useScopeAccess(`org:${organizationId}`)
  const canManage = can("agents:manage")
  // Each action says what failed itself.
  const make = useCreateChatAgent(organizationId)
  const remove = useDeleteChatAgent(organizationId)
  const [importing, setImporting] = React.useState(false)
  const [removing, setRemoving] = React.useState<AgentRow>()
  const context = React.useMemo<ChatAgentValidationContext>(
    () => ({
      agents: new Map(
        agents.map((a) => [a.id, { name: a.name, uses: usesOf(a) }])
      ),
      // Unknown until they load: a pick isn't called deleted meanwhile.
      workflows: workflowsKnown
        ? new Map(workflows.map((w) => [w.id, { name: w.name }]))
        : undefined,
    }),
    [agents, workflows, workflowsKnown]
  )
  const rows = React.useMemo(
    () => records.map((r) => toRow(r, context)),
    [records, context]
  )

  const open = React.useCallback(
    (id: string) =>
      void navigate({
        to: "/organizations/$organizationId/chat-agents/$chatAgentId",
        params: { organizationId, chatAgentId: id },
      }),
    [navigate, organizationId]
  )
  const makeAsync = make.mutateAsync
  const keep = React.useCallback(
    async (doc: ChatAgentDocument, failure: string) => {
      try {
        const made = await makeAsync(doc)
        open(made.id)
      } catch (caught) {
        toast.add({
          title: failure,
          description: toApiError(caught).message,
          type: "error",
        })
      }
    },
    [makeAsync, open]
  )
  const create = () =>
    void keep(
      newChatAgent(organizationId, freshName("Untitled agent", agents)),
      "Couldn't create the agent"
    )
  const startFromExample = () =>
    void keep(exampleChatAgent(organizationId), "Couldn't add the example")

  const actions = React.useMemo<RowActions>(
    () => ({
      open: (row) => open(row.id),
      ...(canManage
        ? {
            duplicate: (row: AgentRow) =>
              void keep(
                copyChatAgent(row.doc, `${row.name} (copy)`),
                "Couldn't duplicate the agent"
              ),
            remove: setRemoving,
          }
        : {}),
    }),
    [canManage, keep, open]
  )
  const parse = React.useCallback(
    (text: string) => parseChatAgent(text, { organizationId }),
    [organizationId]
  )
  const busy = make.isPending

  return (
    <>
      {canManage && (
        <ShellHeaderActions>
          <Button
            variant="outline"
            disabled={busy}
            onClick={() => setImporting(true)}
          >
            <Icon icon="file" data-icon="inline-start" />
            Import
          </Button>
          <PrimaryAction disabled={busy} onClick={create}>
            Agent
          </PrimaryAction>
        </ShellHeaderActions>
      )}

      {list.error ? (
        <ErrorCallout
          title="Couldn't load the organization's agents"
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
      ) : !list.isPending && rows.length === 0 ? (
        <EmptyWorkspace
          illustration={<EmptyIllustration name="waiting" />}
          title={`Build ${organizationName}'s agents`}
          description="An agent is one Google ADK chat agent: what it's told and the model it runs on, the tools it can call (memory, HTTP endpoints, OpenAPI and MCP servers, your ADK workflows) and the sub-agents it hands off to, connected on a canvas. For now they're kept in this browser."
          actions={
            canManage ? (
              <>
                <Button variant="outline" disabled={busy} onClick={create}>
                  New agent
                </Button>
                <Button
                  variant="outline"
                  disabled={busy}
                  onClick={startFromExample}
                >
                  Start from the example
                </Button>
              </>
            ) : undefined
          }
          steps={[
            { icon: "robot", label: "Set up the agent" },
            { icon: "branch", label: "Connect its tools" },
            { icon: "code", label: "Export the JSON" },
          ]}
        />
      ) : (
        <ActionsContext.Provider value={actions}>
          <DataTable
            className="rounded-none border-0"
            title="Agents"
            description={`The chat agents built for ${organizationName} in this browser. Open one to build it.`}
            columns={COLUMNS}
            data={rows}
            isLoading={list.isPending}
            getRowId={(row) => row.id}
            features={FEATURES}
            initialState={INITIAL_STATE}
            stateKey="organization-chat-agents"
            exportFileName="agents"
            labels={{ rows: "agents", rowSingular: "agent" }}
            onRowClick={(row) => open(row.original.id)}
          />
        </ActionsContext.Provider>
      )}

      <ImportDialog
        open={importing}
        onOpenChange={setImporting}
        format={CHAT_AGENT_FORMAT}
        nouns={{ doc: "agent", steps: "nodes" }}
        parse={parse}
        title="Import an agent"
        description={`Adds it to ${organizationName}'s agents and opens it. It gets a new ID, so it never replaces one you have.`}
        action="Import and open"
        onImport={(doc) =>
          void keep(
            copyChatAgent(doc, freshName(doc.name, agents)),
            "Couldn't import the agent"
          )
        }
      />

      <Dialog
        open={Boolean(removing)}
        onOpenChange={(next) => !next && setRemoving(undefined)}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete {removing?.name}?</DialogTitle>
            <DialogDescription>
              It's removed from this browser, with everything attached to it.
              Agents that use it as a saved agent will need another. You can't
              undo this; export its JSON first to keep a copy.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <DialogClose render={<Button type="button" variant="outline" />}>
              Cancel
            </DialogClose>
            <Button
              variant="destructive"
              disabled={remove.isPending}
              onClick={async () => {
                if (!removing) return
                try {
                  await remove.mutateAsync(removing.id)
                  toast.add({
                    title: `Deleted ${removing.name}`,
                    type: "success",
                  })
                  setRemoving(undefined)
                } catch (caught) {
                  toast.add({
                    title: "Couldn't delete the agent",
                    description: toApiError(caught).message,
                    type: "error",
                  })
                }
              }}
            >
              Delete agent
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  )
}
