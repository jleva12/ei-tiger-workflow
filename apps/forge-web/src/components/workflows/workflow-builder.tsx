import * as React from "react"
import { Link, useNavigate } from "@tanstack/react-router"
import { PlayIcon } from "@hugeicons/core-free-icons"
import { ReactFlowProvider, useReactFlow } from "@xyflow/react"
import { cn } from "cn"

import { BuilderCanvas } from "@/components/builder/canvas"
import { BuilderToolbar, IssuesButton, SaveStatus } from "@/components/builder/chrome"
import { DetailsResizer } from "@/components/builder/details-resizer"
import { BuilderJson } from "@/components/builder/json-view"
import { StepLibrary } from "@/components/builder/library"
import { StepDialog } from "@/components/builder/step-dialog"
import { BuilderUiContext } from "@/components/builder/ui"
import {
  DETAILS_PANEL_ID,
  downloadJson,
  measureFlow,
  useDetailsWidth,
  useUndoKeys,
} from "@/components/builder/utils"
import { PrimaryAction } from "@/components/forge/app-shell"
import { PageEmpty } from "@/components/forge/empty-state"
import { ErrorCallout } from "@/components/forge/feedback"
import { Icon } from "@/components/forge/icon"
import { ShellHeaderActions, ShellToolbar, useShellPage } from "@/components/forge/shell"
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
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { Skeleton } from "@/components/ui/skeleton"
import { Spinner } from "@/components/ui/spinner"
import { toast } from "@/components/ui/toast"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { useMyOrganizations, type MyOrganization } from "@/lib/access"
import { toApiError } from "@/lib/api"
import { useScopeAccess } from "@/lib/hierarchy"
import { usePageContext } from "@/lib/page-context"
import {
  organizationWorkflows,
  useOrganizationWorkflows,
  type WorkflowRecord,
} from "@/lib/workflows/api"
import { copyWorkflow, WORKFLOW_FORMAT, type WorkflowDocument } from "@/lib/workflows/document"
import { useAgentStepModels } from "@/lib/workflows/models"
import { WORKFLOWS_ICON } from "@/lib/workflows/model"
import { WORKFLOW_JSON_SCHEMA } from "@/lib/workflows/schema"
import { useAutosave } from "./autosave"
import {
  BuilderProvider,
  documentOf,
  useBuilder,
  useBuilderApi,
  type StepEdge,
  type StepNode,
} from "./builder-store"
import { workflowFileName } from "./builder-utils"
import { ImportDialog } from "./import-dialog"
import { WorkflowRunDialog } from "./run-dialog"
import { WorkflowRunsMenu } from "./runs-menu"
import { WorkflowDetails } from "./workflow-details"
import { WORKFLOW_UI } from "./workflow-ui"

/**
 * A workflow's builder, for the organization's members: the step library in the
 * sidebar, the canvas, the workflow's details on the right, each step's
 * settings in a dialog over the canvas, and the workflow's JSON a tab away.
 * Changes are saved to the organization as they're made (see ./autosave).
 */
export function WorkflowBuilderPage({
  organizationId,
  workflowId,
}: {
  organizationId: string
  workflowId: string
}) {
  const myOrganizations = useMyOrganizations()
  const organization = myOrganizations.data?.find((t) => t.id === organizationId)
  // Always the latest on opening: the builder saves from the revision it starts at.
  const workflow = organizationWorkflows.scope({ organizationId }).useDetail(organization ? workflowId : undefined, {
    refetchOnMount: "always",
    refetchOnWindowFocus: false,
    retry: (count, error) => toApiError(error).status !== 404 && count < 2,
  })
  // What the builder opened. From then on it owns the document: a later
  // refetch, even a failed one, never closes it (and what's unsaved with it).
  const [record, setRecord] = React.useState<WorkflowRecord>()
  if (!record && workflow.isFetchedAfterMount && workflow.isSuccess) setRecord(workflow.data)

  useShellPage({
    header: {
      title: workflow.data?.document.name ?? "Workflow",
      icon: WORKFLOWS_ICON,
      breadcrumbs: organization
        ? [
            { label: organization.name, href: `/organizations/${organizationId}` },
            { label: "Workflows", href: `/organizations/${organizationId}?view=workflows` },
          ]
        : [],
    },
    // The builder draws its own sub nav (the step library), not the workspace's.
    sidebar: { label: "Workflow steps", sections: [] },
  })
  usePageContext({
    entities: [
      { kind: "organization", id: organizationId, label: organization?.name },
      { kind: "workflow", id: workflowId, label: workflow.data?.document.name },
    ],
  })

  if (myOrganizations.isPending || (organization && !record && !workflow.error)) {
    return (
      <div className="flex flex-col gap-3" aria-busy="true">
        <Skeleton className="h-8 w-64" />
        <Skeleton className="h-[60vh] w-full rounded-(--radius-card)" />
      </div>
    )
  }
  // Only a first load's error replaces the page; a failed refetch keeps what loaded.
  if (myOrganizations.isLoadingError) {
    return (
      <ErrorCallout
        title="Couldn't load your organizations"
        action={
          <Button variant="outline" size="sm" onClick={() => void myOrganizations.refetch()}>
            Retry
          </Button>
        }
      >
        {myOrganizations.error.message}
      </ErrorCallout>
    )
  }
  if (!organization) {
    return (
      <PageEmpty
        illustration="locked"
        title="You're not in this organization"
        description="Only an organization's members can open its workflows. Pick one of your organizations at the top of the sidebar, or ask one of the organization's admins to add you."
      />
    )
  }
  if (!record && workflow.error && toApiError(workflow.error).status !== 404) {
    return (
      <ErrorCallout
        title="Couldn't open the workflow"
        action={
          <Button variant="outline" size="sm" onClick={() => void workflow.refetch()}>
            Retry
          </Button>
        }
      >
        {workflow.error.message}
      </ErrorCallout>
    )
  }
  if (!record) {
    return (
      <PageEmpty
        illustration="search"
        title="Workflow not found"
        description="The organization has no workflow here: it may have been deleted, or the link is to another organization's."
      >
        <Button
          variant="outline"
          nativeButton={false}
          render={<Link to="/organizations/$organizationId" params={{ organizationId }} search={{ view: "workflows" }} />}
        >
          All workflows
        </Button>
      </PageEmpty>
    )
  }
  const doc = record.document
  return (
    <BuilderProvider key={record.id} initial={{ doc, context: { selfId: record.id } }}>
      <BuilderUiContext.Provider value={WORKFLOW_UI}>
        <ReactFlowProvider>
          <Builder
            organization={organization}
            record={record}
            needsLayout={doc.nodes.some((n) => !doc.layout[n.id])}
          />
        </ReactFlowProvider>
      </BuilderUiContext.Provider>
    </BuilderProvider>
  )
}

/** Names for the IDs steps hold, and what Run workflow steps may pick. */
function useLookups(organizationId: string) {
  const api = useBuilderApi()
  const { workflows } = useOrganizationWorkflows(organizationId)
  const { models, defaultModel } = useAgentStepModels()
  React.useEffect(() => {
    const names = Object.fromEntries(models.map((m) => [m.id, m.name]))
    const fallback = defaultModel ? names[defaultModel] : undefined
    api.getState().setLookups({ models: fallback ? { ...names, "": fallback } : names })
  }, [api, models, defaultModel])
  React.useEffect(() => {
    const state = api.getState()
    state.setLookups({ workflows: Object.fromEntries(workflows.map((w) => [w.id, w.name])) })
    state.setContext({ selfId: state.meta.id, workflowIds: new Set(workflows.map((w) => w.id)) })
  }, [api, workflows])
}

function Builder({
  organization,
  record,
  needsLayout,
}: {
  organization: MyOrganization
  record: WorkflowRecord
  needsLayout: boolean
}) {
  const api = useBuilderApi()
  const flow = useReactFlow<StepNode, StepEdge>()
  const navigate = useNavigate()
  const view = useBuilder((s) => s.view)
  const name = useBuilder((s) => s.meta.name)
  // The details sit beside the canvas (over it when narrow), only when asked for.
  const details = useBuilder((s) => s.details)
  const [importing, setImporting] = React.useState(false)
  const [deleting, setDeleting] = React.useState(false)
  const [running, setRunning] = React.useState<Record<string, unknown>>()
  const [detailsWidth, setDetailsWidth] = useDetailsWidth("forge.workflows.detailsWidth")
  const saving = useAutosave(organization.id, record)
  const scoped = organizationWorkflows.scope({ organizationId: organization.id })
  const make = scoped.useCreate({ meta: { errorTitle: "Couldn't duplicate the workflow" } })
  const drop = scoped.useDelete({ meta: { errorTitle: "Couldn't delete the workflow" } })
  useUndoKeys()
  useLookups(organization.id)

  useShellPage({ header: { title: name || "Untitled workflow" } })

  const canRun = useScopeAccess(`org:${organization.id}`)("workflows:run")
  const errors = useBuilder((s) => s.issues.filter((i) => i.level === "error").length)
  // A run takes the workflow as it's saved: only once what's shown is.
  const unsaved = saving.state.kind !== "saved"
  const runBlocked = errors
    ? `Fix the ${errors === 1 ? "problem" : `${errors} problems`} first: a run would stop at ${errors === 1 ? "it" : "them"}.`
    : unsaved
      ? "It runs as saved: wait until your changes are."
      : undefined
  const startRun = () => {
    const doc = documentOf(api.getState())
    const entry = doc.nodes.find((node) => node.id === doc.entry)
    setRunning(entry?.kind === "entry" ? (entry.config.input_schema ?? {}) : {})
  }
  const started = () =>
    toast.add({
      title: "Run started.",
      description: "It shows under Runs, and in Workflow tasks, once a worker picks it up.",
      type: "success",
      actionProps: {
        children: "Workflow tasks",
        onClick: () =>
          void navigate({
            to: "/organizations/$organizationId",
            params: { organizationId: organization.id },
            search: { view: "workflows", workflowsTab: "tasks" },
          }),
      },
    })

  const exportJson = () => {
    const doc = documentOf(api.getState())
    downloadJson(doc, workflowFileName(doc))
  }
  const duplicate = () => {
    const doc = documentOf(api.getState())
    make.mutate(
      { document: copyWorkflow(doc, `${doc.name} (copy)`) },
      {
        onSuccess: (copy) =>
          void navigate({
            to: "/organizations/$organizationId/workflows/$workflowId",
            params: { organizationId: organization.id, workflowId: copy.id },
          }),
      }
    )
  }
  const remove = () => {
    drop.mutate(api.getState().meta.id, {
      onSuccess: () => {
        toast.add({ title: `Deleted ${name}`, type: "success" })
        void navigate({
          to: "/organizations/$organizationId",
          params: { organizationId: organization.id },
          search: { view: "workflows" },
        })
      },
    })
  }

  return (
    <>
      <StepLibrary organizationId={organization.id} />

      <ShellHeaderActions>
        <SaveStatus saving={saving} organizationName={organization.name} />
        <IssuesButton />
        <WorkflowRunsMenu organizationId={organization.id} workflowId={record.id} />
        <DropdownMenu>
          <DropdownMenuTrigger
            render={<Button variant="ghost" size="icon" aria-label="More workflow actions" />}
          >
            <Icon icon="more" />
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="w-56">
            <DropdownMenuItem onClick={exportJson}>
              <Icon icon="download" />
              Export JSON
            </DropdownMenuItem>
            <DropdownMenuItem onClick={duplicate}>
              <Icon icon="copy" />
              Duplicate workflow
            </DropdownMenuItem>
            <DropdownMenuItem onClick={() => setImporting(true)}>
              <Icon icon="file" />
              Replace from JSON…
            </DropdownMenuItem>
            <DropdownMenuItem
              onClick={() => downloadJson(WORKFLOW_JSON_SCHEMA, "forge-workflow.schema.json")}
            >
              <Icon icon="download" />
              Download the JSON Schema
            </DropdownMenuItem>
            <DropdownMenuSeparator />
            <DropdownMenuItem variant="destructive" onClick={() => setDeleting(true)}>
              Delete workflow…
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
        {canRun &&
          (runBlocked ? (
            <Tooltip>
              <TooltipTrigger render={<span tabIndex={0} className="rounded-(--radius-control)" />}>
                <PrimaryAction icon={PlayIcon} disabled>
                  Run
                </PrimaryAction>
              </TooltipTrigger>
              <TooltipContent className="max-w-72">{runBlocked}</TooltipContent>
            </Tooltip>
          ) : (
            <PrimaryAction icon={PlayIcon} onClick={startRun}>
              Run
            </PrimaryAction>
          ))}
      </ShellHeaderActions>
      {canRun && (
        <WorkflowRunDialog
          open={running !== undefined}
          onOpenChange={(open) => !open && setRunning(undefined)}
          organizationId={organization.id}
          workflowId={record.id}
          name={name}
          inputSchema={running ?? {}}
          onStarted={started}
        />
      )}

      <ShellToolbar>
        <BuilderToolbar
          details={details}
          onDetails={() => api.getState().setDetails(!details)}
        />
      </ShellToolbar>

      {/* The panel is as wide as its viewer left it, up to half the builder;
          tucked away (as it opens), the canvas has the whole width. */}
      <div
        className={cn(
          "absolute inset-0 grid grid-cols-1",
          details &&
            "grid-cols-[minmax(0,1fr)_min(var(--details-width),50%)] @max-[900px]/shell:grid-cols-1"
        )}
        style={{ "--details-width": `${detailsWidth}rem` } as React.CSSProperties}
      >
        <div
          className={cn("relative min-h-0 min-w-0", view === "json" && "invisible")}
          inert={view === "json" || undefined}
        >
          <BuilderCanvas needsLayout={needsLayout} />
        </div>
        <aside
          id={DETAILS_PANEL_ID}
          aria-label="Workflow details"
          inert={view === "json" || undefined}
          className={cn(
            "relative flex min-h-0 flex-col border-l bg-background",
            view === "json" && "invisible",
            "@max-[900px]/shell:absolute @max-[900px]/shell:top-3 @max-[900px]/shell:right-3 @max-[900px]/shell:bottom-3 @max-[900px]/shell:z-10 @max-[900px]/shell:w-[min(var(--details-width),calc(100%-1.5rem))] @max-[900px]/shell:rounded-(--radius-card) @max-[900px]/shell:border @max-[900px]/shell:shadow-(--shadow-float)",
            !details && "hidden"
          )}
        >
          <DetailsResizer
            width={detailsWidth}
            onWidthChange={setDetailsWidth}
            className="@max-[900px]/shell:hidden"
          />
          <div className="min-h-0 flex-1 overflow-y-auto">
            <WorkflowDetails />
          </div>
          {/* Kept clear for the assistant's launcher, which floats here. */}
          <div aria-hidden="true" className="h-18 shrink-0" />
        </aside>
        {view === "json" && (
          <div className="absolute inset-0 bg-background">
            <BuilderJson<WorkflowDocument>
              format={WORKFLOW_FORMAT}
              schema={WORKFLOW_JSON_SCHEMA}
              schemaFileName="forge-workflow.schema.json"
              fileName={workflowFileName}
              about={
                <>
                  The graph a runner executes: <code className="font-mono text-[0.9em]">nodes</code> are
                  the steps, <code className="font-mono text-[0.9em]">edges</code> join a step&apos;s
                  output to the next step, and <code className="font-mono text-[0.9em]">entry</code> is
                  where every run starts. <code className="font-mono text-[0.9em]">layout</code> is only
                  for this builder.
                </>
              }
              onImport={() => setImporting(true)}
            />
          </div>
        )}
      </div>

      <StepDialog />

      <ImportDialog
        open={importing}
        onOpenChange={setImporting}
        organizationId={organization.id}
        title="Replace from JSON"
        description="Its steps and connections replace what's on the canvas; the name and description come with them. Undo puts the canvas back."
        action="Replace the canvas"
        onImport={(doc, needsLayout) => {
          const store = api.getState()
          store.replaceDocument(doc)
          store.setView("canvas")
          if (needsLayout) {
            requestAnimationFrame(() => {
              api.getState().arrange(measureFlow(flow))
              api.getState().requestFit()
            })
          } else store.requestFit()
        }}
      />

      <Dialog open={deleting} onOpenChange={setDeleting}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete {name}?</DialogTitle>
            <DialogDescription>
              It's removed for everyone in {organization.name}, with its steps and connections.
              You can't undo this; export its JSON first to keep a copy.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <DialogClose render={<Button type="button" variant="outline" />}>
              Cancel
            </DialogClose>
            <Button variant="destructive" disabled={drop.isPending} onClick={remove}>
              {drop.isPending && <Spinner data-icon="inline-start" />}
              Delete workflow
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  )
}
