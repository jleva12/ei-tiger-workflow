import * as React from "react"
import { useNavigate } from "@tanstack/react-router"

import {
  ADMIN_TABLE_FEATURES,
  PIN_ACTIONS,
} from "@/features/admin/components/table-config"
import { RowMenu } from "@/features/admin/components/table-parts"
import { ImportDialog } from "@/features/builder/components/import-dialog"
import { downloadJson } from "@/features/builder/components/utils"
import {
  PrimaryAction,
  ToolbarFilters,
  ViewToolbar,
} from "@/components/forge/app-shell"
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
import { ShellHeaderActions, ShellToolbar } from "@/components/forge/shell/index"
import { Chip } from "@/components/forge/status"
import { ViewTabsList, ViewTabsTrigger } from "@/components/forge/toolbar"
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
import { Spinner } from "@/components/ui/spinner"
import { Tabs, TabsContent } from "@/components/ui/tabs"
import { toast } from "@/components/ui/toast"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import {
  RunList,
  type RunListWords,
} from "@/features/runs/components/run-list"
import {
  organizationAgents,
  useOrganizationAgents,
  type AgentRecord,
} from "@/features/adk-workflows/lib/api"
import {
  AGENT_FORMAT,
  copyAgent,
  newAgent,
  parseAgent,
  storedAgent,
  toGraph,
  usesOf,
  type AgentDocument,
} from "@/features/adk-workflows/lib/document"
import { exampleAgent } from "@/features/adk-workflows/lib/example"
import { AGENTS_ICON, countAgents } from "@/features/adk-workflows/lib/model"
import {
  isAgentsTab,
  useOrganizationAdkRuns,
  type AgentsTab,
} from "@/features/runs/lib/runs"
import {
  validateAgent,
  type AgentValidationContext,
} from "@/features/adk-workflows/lib/validate"
import { toApiError } from "@/lib/api/index"
import { formatRelative } from "@/lib/format"
import { useScopeAccess } from "@/lib/hierarchy"
import { parseTimestamp } from "@/lib/timestamps"
import { agentFileName } from "./agent-files"

/** A row of the table: one agent. */
type AgentRow = {
  id: string
  name: string
  description: string
  nodes: number
  /** Its ADK agents: the agent nodes and their sub-agents. */
  agents: number
  errors: number
  warnings: number
  updated_at: string
  /** Who saved it last. */
  updated_by: string
  doc: AgentDocument
}

const toRow = (
  record: AgentRecord,
  context: AgentValidationContext
): AgentRow => {
  const doc = storedAgent(record.document, record.organization_id)
  const graph = toGraph(doc)
  const issues = validateAgent(graph, { ...context, selfId: doc.id })
  const errors = issues.filter((i) => i.level === "error").length
  const agents = countAgents(graph.steps.map((s) => s.data))
  return {
    id: doc.id,
    name: doc.name,
    description: doc.description,
    nodes: doc.nodes.length,
    agents,
    errors,
    warnings: issues.length - errors,
    updated_at: record.updated_at,
    updated_by: record.updated_by_name ?? record.updated_by,
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
    header: "Workflow",
    size: 320,
    enableHiding: false,
    meta: { label: "Workflow" },
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
  helper.accessor("nodes", {
    header: "Nodes",
    size: 180,
    meta: { label: "Nodes", align: "right" },
    cell: ({ row: { original: row } }) => (
      <span className="tabular-nums">
        {plural(row.nodes, "node")}
        {row.agents > 0 && (
          <span className="text-muted-foreground">{` · ${plural(row.agents, "agent")}`}</span>
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
      editIcon={AGENTS_ICON}
      onDelete={actions.remove && (() => actions.remove?.(row))}
    >
      {actions.duplicate && (
        <DropdownMenuItem onClick={() => actions.duplicate?.(row)}>
          <Icon icon="copy" />
          Duplicate
        </DropdownMenuItem>
      )}
      <DropdownMenuItem
        onClick={() => downloadJson(row.doc, agentFileName(row.doc))}
      >
        <Icon icon="download" />
        Export JSON
      </DropdownMenuItem>
    </RowMenu>
  )
}

// The Runs tab, in the words of what runs.
const RUN_WORDS: RunListWords = {
  title: "Workflow runs",
  list: "Workflow runs",
  docs: "Workflows",
  aDoc: "a workflow",
  icon: AGENTS_ICON,
}

/** A tab's count, quieter than its label. */
function TabCount({ value }: { value: number | undefined }) {
  if (value === undefined) return null
  return (
    <span className="text-2xs text-subtle tabular-nums">
      {value.toLocaleString()}
    </span>
  )
}

/** "Untitled workflow", or "Untitled workflow 2" when that's taken. */
function freshName(base: string, taken: AgentDocument[]) {
  const names = new Set(taken.map((a) => a.name))
  if (!names.has(base)) return base
  for (let n = 2; ; n += 1)
    if (!names.has(`${base} ${n}`)) return `${base} ${n}`
}

/**
 * An organization's workflows page, in two tabs. Overview lists the
 * Google ADK graph workflows it has built, each opening its builder. New
 * Workflow starts one from its start; the example and an import start
 * from more. The organization keeps them (the admin API, in MongoDB), so
 * everyone in it sees and reuses the same ones. Runs lists their runs by
 * status, each opening its own page.
 */
export function OrganizationAgents({
  organizationId,
  organizationName,
  tab,
  onTabChange,
  onOpenRun,
}: {
  organizationId: string
  organizationName: string
  tab: AgentsTab
  onTabChange: (tab: AgentsTab) => void
  /** Open a run's page (its ID). */
  onOpenRun: (taskId: string) => void
}) {
  const navigate = useNavigate()
  const list = useOrganizationAgents(organizationId)
  // Also on Overview, for the tab's count.
  const runs = useOrganizationAdkRuns(organizationId)
  const { records, agents } = list
  const can = useScopeAccess(`org:${organizationId}`)
  const canManage = can("agents:manage")
  const scoped = organizationAgents.scope({ organizationId })
  // Each action says what failed itself.
  const make = scoped.useCreate({ meta: { silent: true } })
  const remove = scoped.useDelete({ meta: { silent: true } })
  const [importing, setImporting] = React.useState(false)
  const [removing, setRemoving] = React.useState<AgentRow>()
  const context = React.useMemo<AgentValidationContext>(
    () => ({
      agents: new Map(
        agents.map((a) => [a.id, { name: a.name, uses: usesOf(a) }])
      ),
    }),
    [agents]
  )
  const rows = React.useMemo(
    () => records.map((r) => toRow(r, context)),
    [records, context]
  )

  const open = React.useCallback(
    (id: string) =>
      void navigate({
        to: "/organizations/$organizationId/agents/$agentId",
        params: { organizationId, agentId: id },
      }),
    [navigate, organizationId]
  )
  const makeAsync = make.mutateAsync
  const keep = React.useCallback(
    async (doc: AgentDocument, failure: string) => {
      try {
        const made = await makeAsync({ document: doc })
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
      newAgent(organizationId, freshName("Untitled workflow", agents)),
      "Couldn't create the workflow"
    )
  const startFromExample = () =>
    void keep(exampleAgent(organizationId), "Couldn't add the example")

  const actions = React.useMemo<RowActions>(
    () => ({
      open: (row) => open(row.id),
      ...(canManage
        ? {
            duplicate: (row: AgentRow) =>
              void keep(
                copyAgent(row.doc, `${row.name} (copy)`),
                "Couldn't duplicate the workflow"
              ),
            remove: setRemoving,
          }
        : {}),
    }),
    [canManage, keep, open]
  )
  const parse = React.useCallback(
    (text: string) => parseAgent(text, { organizationId }),
    [organizationId]
  )
  const busy = make.isPending

  // Only a refresh asked for shows; the list's own polling doesn't.
  const [refreshing, setRefreshing] = React.useState(false)
  function refresh() {
    setRefreshing(true)
    void runs.refetch().then((result) => {
      setRefreshing(false)
      if (result.isSuccess)
        toast.add({ title: "Workflow runs refreshed.", type: "info" })
    })
  }

  return (
    <Tabs
      value={tab}
      onValueChange={(value) => {
        if (isAgentsTab(value)) onTabChange(value)
      }}
      className="gap-0"
    >
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
            Workflow
          </PrimaryAction>
        </ShellHeaderActions>
      )}

      <ShellToolbar>
        <ViewToolbar>
          <ViewTabsList aria-label="Workflows">
            <ViewTabsTrigger value="overview" icon={AGENTS_ICON}>
              Overview
              <TabCount value={list.isPending ? undefined : rows.length} />
            </ViewTabsTrigger>
            <ViewTabsTrigger value="runs" icon="task">
              Runs
              <TabCount value={runs.data?.total} />
            </ViewTabsTrigger>
          </ViewTabsList>
          <ToolbarFilters>
            {tab === "runs" && (
              <Tooltip>
                <TooltipTrigger
                  render={
                    <Button
                      variant="outline"
                      size="icon"
                      aria-label="Refresh workflow runs"
                      disabled={refreshing}
                      onClick={refresh}
                    />
                  }
                >
                  {refreshing ? <Spinner /> : <Icon icon="refresh" size={15} />}
                </TooltipTrigger>
                <TooltipContent>Refresh workflow runs</TooltipContent>
              </Tooltip>
            )}
          </ToolbarFilters>
        </ViewToolbar>
      </ShellToolbar>

      <TabsContent value="overview">
        {list.error ? (
          <ErrorCallout
            title="Couldn't load the organization's workflows"
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
            title={`Build ${organizationName}'s workflows`}
            description="A workflow is a Google ADK graph: LLM agents and their teams (sequential, parallel, loop), with Forge's steps between them: HTTP requests, transforms, approvals and people's answers, If / Switch, loops and merges. Build it on a canvas; it's saved as a JSON graph ADK builds."
            actions={
              canManage ? (
                <>
                  <Button variant="outline" disabled={busy} onClick={create}>
                    New workflow
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
              { icon: "robot", label: "Drop in agents" },
              { icon: "branch", label: "Route between them" },
              { icon: "code", label: "Export the JSON" },
            ]}
          />
        ) : (
          <ActionsContext.Provider value={actions}>
            <DataTable
              className="rounded-none border-0"
              title="Workflows"
              description={`Every workflow ${organizationName} has built, shared by everyone in the organization. Open one to build it.`}
              columns={COLUMNS}
              data={rows}
              isLoading={list.isPending}
              getRowId={(row) => row.id}
              features={FEATURES}
              initialState={INITIAL_STATE}
              stateKey="organization-agents"
              exportFileName="adk-workflows"
              labels={{ rows: "Workflows", rowSingular: "Workflow" }}
              onRowClick={(row) => open(row.original.id)}
            />
          </ActionsContext.Provider>
        )}
      </TabsContent>

      <TabsContent value="runs">
        <RunList
          organizationName={organizationName}
          runs={runs}
          words={RUN_WORDS}
          onOpenRun={onOpenRun}
          onShowOverview={() => onTabChange("overview")}
        />
      </TabsContent>

      <ImportDialog
        open={importing}
        onOpenChange={setImporting}
        format={AGENT_FORMAT}
        nouns={{ doc: "Workflow", steps: "nodes" }}
        parse={parse}
        title="Import a workflow"
        description={`Adds it to ${organizationName}'s workflows and opens it. It gets a new ID, so it never replaces one you have.`}
        action="Import and open"
        onImport={(doc) =>
          void keep(
            copyAgent(doc, freshName(doc.name, agents)),
            "Couldn't import the workflow"
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
              It's removed for everyone in {organizationName}, with its nodes
              and edges. Workflows that run it as a saved workflow will need another.
              You can't undo this; export its JSON first to keep a copy.
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
                    title: "Couldn't delete the workflow",
                    description: toApiError(caught).message,
                    type: "error",
                  })
                }
              }}
            >
              Delete workflow
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </Tabs>
  )
}
