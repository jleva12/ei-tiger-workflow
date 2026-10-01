import * as React from "react"
import { useNavigate } from "@tanstack/react-router"

import { ADMIN_TABLE_FEATURES, PIN_ACTIONS } from "@/components/admin/table-config"
import { RowMenu } from "@/components/admin/table-parts"
import { PrimaryAction, ToolbarFilters, ViewToolbar } from "@/components/forge/app-shell"
import {
  createColumnHelper,
  DataTable,
  type DataTableFeatureConfig,
  type InitialTableState,
} from "@/components/forge/data-table"
import { EmptyIllustration, EmptyWorkspace } from "@/components/forge/empty-state"
import { ErrorCallout } from "@/components/forge/feedback"
import { Icon } from "@/components/forge/icon"
import { ShellHeaderActions, ShellToolbar } from "@/components/forge/shell"
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
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { toApiError } from "@/lib/api"
import { formatRelative } from "@/lib/format"
import { useScopeAccess } from "@/lib/hierarchy"
import {
  copyWorkflow,
  newWorkflow,
  toGraph,
  type WorkflowDocument,
} from "@/lib/workflows/document"
import { exampleWorkflow } from "@/lib/workflows/example"
import { STEP_KINDS, WORKFLOWS_ICON } from "@/lib/workflows/model"
import { inputFields } from "@/lib/workflows/scope"
import {
  organizationWorkflows,
  useOrganizationWorkflows,
  type WorkflowRecord,
} from "@/lib/workflows/api"
import { isWorkflowsTab, useOrganizationWorkflowRuns, type WorkflowsTab } from "@/lib/workflows/runs"
import { validateWorkflow } from "@/lib/workflows/validate"
import { parseTimestamp } from "@/lib/timestamps"
import { ImportDialog } from "./import-dialog"
import { downloadJson, workflowFileName } from "./builder-utils"
import { WorkflowTasks } from "./workflow-tasks"

/** A row of the table: one workflow. */
type WorkflowRow = {
  id: string
  name: string
  description: string
  /** The start step's input fields; null without a start step. */
  input: string[] | null
  steps: number
  agents: number
  errors: number
  warnings: number
  updated_at: string
  /** Who saved it last. */
  updated_by: string
  doc: WorkflowDocument
}

const toRow = (record: WorkflowRecord, ids: Set<string>): WorkflowRow => {
  const doc = record.document
  const entry = doc.nodes.find((n) => n.kind === "entry")
  const issues = validateWorkflow(toGraph(doc), { selfId: doc.id, workflowIds: ids })
  const errors = issues.filter((i) => i.level === "error").length
  return {
    id: doc.id,
    name: doc.name,
    description: doc.description,
    input: entry?.kind === "entry" ? inputFields(entry.config.input_schema).names : null,
    steps: doc.nodes.length,
    agents: doc.nodes.filter((n) => STEP_KINDS[n.kind].orb !== undefined).length,
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
// Without workflows:manage there's only opening and exporting.
type RowActions = {
  open: (row: WorkflowRow) => void
  duplicate?: (row: WorkflowRow) => void
  remove?: (row: WorkflowRow) => void
}
const ActionsContext = React.createContext<RowActions | undefined>(undefined)

const helper = createColumnHelper<WorkflowRow>()
const COLUMNS = helper.columns([
  helper.accessor("name", {
    header: "Workflow",
    size: 300,
    enableHiding: false,
    meta: { label: "Workflow" },
    cell: ({ row: { original: row } }) => (
      <span className="flex min-w-0 flex-col">
        <span className="truncate text-[0.8125rem] font-medium text-foreground">{row.name}</span>
        <span className="truncate text-2xs text-muted-foreground">
          {row.description || "No description"}
        </span>
      </span>
    ),
  }),
  helper.accessor((row) => row.input?.join(", ") ?? "", {
    id: "input",
    header: "Input",
    size: 200,
    meta: { label: "Input", cellClassName: "text-muted-foreground" },
    cell: ({ row: { original: row } }) =>
      row.input === null ? (
        <span className="text-subtle">No start step</span>
      ) : row.input.length ? (
        <span className="truncate font-mono text-2xs" title={row.input.join(", ")}>
          {row.input.join(", ")}
        </span>
      ) : (
        <span className="text-subtle">Not declared</span>
      ),
  }),
  helper.accessor("steps", {
    header: "Steps",
    size: 160,
    meta: { label: "Steps", align: "right" },
    cell: ({ row: { original: row } }) => (
      <span className="tabular-nums">
        {row.steps} {row.steps === 1 ? "step" : "steps"}
        {row.agents > 0 && (
          <span className="text-muted-foreground">
            {" · "}
            {row.agents} {row.agents === 1 ? "agent" : "agents"}
          </span>
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
        <Chip tone="danger">
          {row.errors} {row.errors === 1 ? "error" : "errors"}
        </Chip>
      ) : row.warnings ? (
        <Chip tone="warning">
          {row.warnings} {row.warnings === 1 ? "warning" : "warnings"}
        </Chip>
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
        <span className="truncate text-2xs text-subtle">by {row.updated_by}</span>
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
    cell: ({ row }) => <WorkflowMenu row={row.original} />,
  }),
])

function WorkflowMenu({ row }: { row: WorkflowRow }) {
  const actions = React.useContext(ActionsContext)
  if (!actions) return null
  return (
    <RowMenu
      label={row.name}
      onEdit={() => actions.open(row)}
      editLabel="Open"
      editIcon={WORKFLOWS_ICON}
      onDelete={actions.remove && (() => actions.remove?.(row))}
    >
      {actions.duplicate && (
        <DropdownMenuItem onClick={() => actions.duplicate?.(row)}>
          <Icon icon="copy" />
          Duplicate
        </DropdownMenuItem>
      )}
      <DropdownMenuItem onClick={() => downloadJson(row.doc, workflowFileName(row.doc))}>
        <Icon icon="download" />
        Export JSON
      </DropdownMenuItem>
    </RowMenu>
  )
}

/** A tab's count, quieter than its label. */
function TabCount({ value }: { value: number | undefined }) {
  if (value === undefined) return null
  return <span className="text-2xs text-subtle tabular-nums">{value.toLocaleString()}</span>
}

/** "Untitled workflow", or "Untitled workflow 2" when that's taken. */
function freshName(base: string, taken: WorkflowDocument[]) {
  const names = new Set(taken.map((w) => w.name))
  if (!names.has(base)) return base
  for (let n = 2; ; n += 1) if (!names.has(`${base} ${n}`)) return `${base} ${n}`
}

/**
 * An organization's Workflows page, in two tabs. Overview lists the agent workflows
 * it has drawn, each opening its builder: New workflow starts one from a
 * start step; the example and an import start from more. The organization keeps
 * them (the admin API, in MongoDB), so everyone in it sees and reuses the
 * same ones. Workflow tasks lists their runs by status, each opening its
 * own page.
 */
export function OrganizationWorkflows({
  organizationId,
  organizationName,
  tab,
  onTabChange,
  onOpenRun,
}: {
  organizationId: string
  organizationName: string
  tab: WorkflowsTab
  onTabChange: (tab: WorkflowsTab) => void
  /** Open a run's page (its background task's ID). */
  onOpenRun: (taskId: string) => void
}) {
  const navigate = useNavigate()
  const list = useOrganizationWorkflows(organizationId)
  // Also on Overview, for the tab's count.
  const runs = useOrganizationWorkflowRuns(organizationId)
  const { records, workflows } = list
  const can = useScopeAccess(`org:${organizationId}`)
  const canManage = can("workflows:manage")
  const scoped = organizationWorkflows.scope({ organizationId })
  // Each action says what failed itself.
  const make = scoped.useCreate({ meta: { silent: true } })
  const remove = scoped.useDelete({ meta: { silent: true } })
  const [importing, setImporting] = React.useState(false)
  const [removing, setRemoving] = React.useState<WorkflowRow>()
  const ids = React.useMemo(() => new Set(workflows.map((w) => w.id)), [workflows])
  const rows = React.useMemo(() => records.map((r) => toRow(r, ids)), [records, ids])

  const open = React.useCallback(
    (id: string) =>
      void navigate({
        to: "/organizations/$organizationId/workflows/$workflowId",
        params: { organizationId, workflowId: id },
      }),
    [navigate, organizationId]
  )
  const makeAsync = make.mutateAsync
  const keep = React.useCallback(
    async (doc: WorkflowDocument, failure: string) => {
      try {
        const made = await makeAsync({ document: doc })
        open(made.id)
      } catch (caught) {
        toast.add({ title: failure, description: toApiError(caught).message, type: "error" })
      }
    },
    [makeAsync, open]
  )
  const create = () =>
    void keep(
      newWorkflow(organizationId, freshName("Untitled workflow", workflows)),
      "Couldn't create the workflow"
    )
  const startFromExample = () =>
    void keep(
      exampleWorkflow(organizationId),
      "Couldn't add the example"
    )

  const actions = React.useMemo<RowActions>(
    () => ({
      open: (row) => open(row.id),
      ...(canManage
        ? {
            duplicate: (row: WorkflowRow) =>
              void keep(
                copyWorkflow(row.doc, `${row.name} (copy)`),
                "Couldn't duplicate the workflow"
              ),
            remove: setRemoving,
          }
        : {}),
    }),
    [canManage, keep, open]
  )

  const importDialog = (
    <ImportDialog
      open={importing}
      onOpenChange={setImporting}
      organizationId={organizationId}
      title="Import a workflow"
      description={`Adds it to ${organizationName}'s workflows and opens it. It gets a new ID, so it never replaces one you have.`}
      action="Import and open"
      onImport={(doc) =>
        void keep(copyWorkflow(doc, freshName(doc.name, workflows)), "Couldn't import the workflow")
      }
    />
  )
  const busy = make.isPending

  // Only a refresh asked for shows; the list's own polling doesn't.
  const [refreshing, setRefreshing] = React.useState(false)
  function refresh() {
    setRefreshing(true)
    void runs.refetch().then((result) => {
      setRefreshing(false)
      if (result.isSuccess) toast.add({ title: "Workflow tasks refreshed.", type: "info" })
    })
  }

  return (
    <Tabs
      value={tab}
      onValueChange={(value) => {
        if (isWorkflowsTab(value)) onTabChange(value)
      }}
      className="gap-0"
    >
      {canManage && (
        <ShellHeaderActions>
          <Button variant="outline" disabled={busy} onClick={() => setImporting(true)}>
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
            <ViewTabsTrigger value="overview" icon={WORKFLOWS_ICON}>
              Overview
              <TabCount value={list.isPending ? undefined : rows.length} />
            </ViewTabsTrigger>
            <ViewTabsTrigger value="tasks" icon="task">
              Workflow tasks
              <TabCount value={runs.data?.total} />
            </ViewTabsTrigger>
          </ViewTabsList>
          <ToolbarFilters>
            {tab === "tasks" && (
              <Tooltip>
                <TooltipTrigger
                  render={
                    <Button
                      variant="outline"
                      size="icon"
                      aria-label="Refresh workflow tasks"
                      disabled={refreshing}
                      onClick={refresh}
                    />
                  }
                >
                  {refreshing ? <Spinner /> : <Icon icon="refresh" size={15} />}
                </TooltipTrigger>
                <TooltipContent>Refresh workflow tasks</TooltipContent>
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
              <Button variant="outline" size="sm" onClick={() => void list.refetch()}>
                Retry
              </Button>
            }
          >
            {list.error.message}
          </ErrorCallout>
        ) : !list.isPending && rows.length === 0 ? (
          <EmptyWorkspace
            illustration={<EmptyIllustration name="waiting" />}
            title={`Draw how work moves through ${organizationName}`}
            description="A workflow joins agents, the systems it calls and the people who approve, from the input that starts it to the result. Build it on a canvas; it's saved as a JSON graph."
            actions={
              canManage ? (
                <>
                  <Button variant="outline" disabled={busy} onClick={create}>
                    New workflow
                  </Button>
                  <Button variant="outline" disabled={busy} onClick={startFromExample}>
                    Start from the example
                  </Button>
                </>
              ) : undefined
            }
            steps={[
              { icon: "sparkles", label: "Drop in steps" },
              { icon: "branch", label: "Wire the branches" },
              { icon: "code", label: "Export the JSON" },
            ]}
          />
        ) : (
          <ActionsContext.Provider value={actions}>
            <DataTable
              className="rounded-none border-0"
              title="Workflow overview"
              description={`Every workflow ${organizationName} has drawn, shared by everyone in the organization. Open one to build it.`}
              columns={COLUMNS}
              data={rows}
              isLoading={list.isPending}
              getRowId={(row) => row.id}
              features={FEATURES}
              initialState={INITIAL_STATE}
              stateKey="organization-workflows"
              exportFileName="workflows"
              labels={{ rows: "workflows", rowSingular: "workflow" }}
              onRowClick={(row) => open(row.original.id)}
            />
          </ActionsContext.Provider>
        )}
      </TabsContent>

      <TabsContent value="tasks">
        <WorkflowTasks
          organizationName={organizationName}
          runs={runs}
          onOpenRun={onOpenRun}
          onShowOverview={() => onTabChange("overview")}
        />
      </TabsContent>

      {importDialog}

      <Dialog open={Boolean(removing)} onOpenChange={(next) => !next && setRemoving(undefined)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete {removing?.name}?</DialogTitle>
            <DialogDescription>
              It's removed for everyone in {organizationName}, with its steps and connections. You
              can't undo this; export its JSON first to keep a copy.
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
                  toast.add({ title: `Deleted ${removing.name}`, type: "success" })
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
