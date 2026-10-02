import * as React from "react"
import { Link, useNavigate } from "@tanstack/react-router"
import { PlayIcon } from "@hugeicons/core-free-icons"
import { useQueryClient } from "@tanstack/react-query"
import { ReactFlowProvider, useReactFlow } from "@xyflow/react"
import { cn } from "cn"

import { useBuilderAutosave } from "@/components/builder/autosave"
import { BuilderCanvas } from "@/components/builder/canvas"
import {
  BuilderToolbar,
  IssuesButton,
  SaveStatus,
} from "@/components/builder/chrome"
import { DetailsResizer } from "@/components/builder/details-resizer"
import { ImportDialog } from "@/components/builder/import-dialog"
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
import {
  ShellHeaderActions,
  ShellToolbar,
  useShellPage,
} from "@/components/forge/shell"
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
import {
  cacheAgent,
  organizationAgents,
  saveAgent,
  useOrganizationAgents,
  type AgentRecord,
} from "@/lib/agents/api"
import {
  AGENT_FORMAT,
  copyAgent,
  parseAgent,
  storedAgent,
  usesOf,
  type AgentDocument,
} from "@/lib/agents/document"
import { AGENTS_ICON } from "@/lib/agents/model"
import type { AdkRun } from "@/lib/agents/runs"
import { AGENT_JSON_SCHEMA } from "@/lib/agents/schema"
import { toApiError } from "@/lib/api"
import { useScopeAccess } from "@/lib/hierarchy"
import { usePageContext } from "@/lib/page-context"
import { useAgentStepModels } from "@/lib/steps/models"
import { AdkRunDialog, AdkRunsMenu } from "./adk-runs"
import { AgentDetails } from "./agent-details"
import { agentFileName } from "./agent-files"
import {
  AgentBuilderProvider,
  agentDocumentOf,
  useAgentBuilder,
  useAgentBuilderApi,
  type AgentEdgeView,
  type AgentNodeView,
} from "./agent-store"
import { AGENT_UI } from "./agent-ui"

const NOUNS = { doc: "ADK workflow", steps: "nodes" }

/**
 * An agent's builder, for the organization's members: the node library in
 * the sidebar, the canvas, the agent's details on the right, each node's
 * settings (and its sub-agents') in a dialog over the canvas, and the
 * agent's JSON a tab away. Changes are saved to the organization as
 * they're made (the builder kit's autosave).
 */
export function AgentBuilderPage({
  organizationId,
  agentId,
}: {
  organizationId: string
  agentId: string
}) {
  const myOrganizations = useMyOrganizations()
  const organization = myOrganizations.data?.find(
    (t) => t.id === organizationId
  )
  // Always the latest on opening: the builder saves from the revision it starts at.
  const agent = organizationAgents
    .scope({ organizationId })
    .useDetail(organization ? agentId : undefined, {
      refetchOnMount: "always",
      refetchOnWindowFocus: false,
      retry: (count, error) => toApiError(error).status !== 404 && count < 2,
    })
  // What the builder opened. From then on it owns the document: a later
  // refetch, even a failed one, never closes it (and what's unsaved with it).
  const [record, setRecord] = React.useState<AgentRecord>()
  if (!record && agent.isFetchedAfterMount && agent.isSuccess)
    setRecord(agent.data)

  useShellPage({
    header: {
      title: agent.data?.document.name ?? "ADK workflow",
      icon: AGENTS_ICON,
      breadcrumbs: organization
        ? [
            {
              label: organization.name,
              href: `/organizations/${organizationId}`,
            },
            {
              label: "ADK workflows",
              href: `/organizations/${organizationId}?view=agents`,
            },
          ]
        : [],
    },
    // The builder draws its own sub nav (the node library), not the workspace's.
    sidebar: { label: "ADK workflow nodes", sections: [] },
  })
  usePageContext({
    entities: [
      { kind: "organization", id: organizationId, label: organization?.name },
    ],
  })

  if (myOrganizations.isPending || (organization && !record && !agent.error)) {
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
          <Button
            variant="outline"
            size="sm"
            onClick={() => void myOrganizations.refetch()}
          >
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
        description="Only an organization's members can open its ADK workflows. Pick one of your organizations at the top of the sidebar, or ask one of the organization's admins to add you."
      />
    )
  }
  if (!record && agent.error && toApiError(agent.error).status !== 404) {
    return (
      <ErrorCallout
        title="Couldn't open the ADK workflow"
        action={
          <Button
            variant="outline"
            size="sm"
            onClick={() => void agent.refetch()}
          >
            Retry
          </Button>
        }
      >
        {agent.error.message}
      </ErrorCallout>
    )
  }
  if (!record) {
    return (
      <PageEmpty
        illustration="search"
        title="ADK workflow not found"
        description="The organization has no ADK workflow here: it may have been deleted, or the link is to another organization's."
      >
        <Button
          variant="outline"
          nativeButton={false}
          render={
            <Link
              to="/organizations/$organizationId"
              params={{ organizationId }}
              search={{ view: "agents" }}
            />
          }
        >
          All ADK workflows
        </Button>
      </PageEmpty>
    )
  }
  return <OpenAgent organization={organization} record={record} />
}

/**
 * The builder, open on an agent. The document is read as an import is: a
 * setting its kind has gained since it was saved is filled in, and one it
 * no longer has is dropped, so it saves as the format stands now.
 */
function OpenAgent({
  organization,
  record,
}: {
  organization: MyOrganization
  record: AgentRecord
}) {
  const [doc] = React.useState(() =>
    storedAgent(record.document, organization.id)
  )
  return (
    <AgentBuilderProvider
      key={record.id}
      initial={{ doc, context: { selfId: record.id } }}
    >
      <BuilderUiContext.Provider value={AGENT_UI}>
        <ReactFlowProvider>
          <Builder
            organization={organization}
            record={record}
            needsLayout={doc.nodes.some((n) => !doc.layout[n.id])}
          />
        </ReactFlowProvider>
      </BuilderUiContext.Provider>
    </AgentBuilderProvider>
  )
}

/** Names for the IDs nodes hold, what saved-agent nodes may pick, and what's checked against them. */
function useLookups(organizationId: string) {
  const api = useAgentBuilderApi()
  const { agents } = useOrganizationAgents(organizationId)
  const { models, defaultModel } = useAgentStepModels()
  React.useEffect(() => {
    const names = Object.fromEntries(models.map((m) => [m.id, m.name]))
    const fallback = defaultModel ? names[defaultModel] : undefined
    api
      .getState()
      .setLookups({ models: fallback ? { ...names, "": fallback } : names })
  }, [api, models, defaultModel])
  React.useEffect(() => {
    const state = api.getState()
    state.setLookups({
      agents: Object.fromEntries(agents.map((a) => [a.id, a.name])),
    })
    state.setContext({
      selfId: state.meta.id,
      agents: new Map(
        agents.map((a) => [a.id, { name: a.name, uses: usesOf(a) }])
      ),
    })
  }, [api, agents])
}

/** Saving the agent to the organization as it's built. */
function useAgentAutosave(organizationId: string, record: AgentRecord) {
  const client = useQueryClient()
  const io = React.useMemo(
    () => ({
      save: (
        id: string,
        body: { document: AgentDocument; revision: number },
        { keepalive }: { keepalive: boolean }
      ) => saveAgent(organizationId, id, body, { keepalive }),
      fetch: (id: string) =>
        organizationAgents.scope({ organizationId }).requests.detail(id),
      cache: (saved: AgentRecord) => cacheAgent(client, saved),
    }),
    [organizationId, client]
  )
  return useBuilderAutosave(record, io)
}

function Builder({
  organization,
  record,
  needsLayout,
}: {
  organization: MyOrganization
  record: AgentRecord
  needsLayout: boolean
}) {
  const api = useAgentBuilderApi()
  const flow = useReactFlow<AgentNodeView, AgentEdgeView>()
  const navigate = useNavigate()
  const view = useAgentBuilder((s) => s.view)
  const name = useAgentBuilder((s) => s.meta.name)
  // The details sit beside the canvas (over it when narrow), only when asked for.
  const details = useAgentBuilder((s) => s.details)
  const [importing, setImporting] = React.useState(false)
  const [deleting, setDeleting] = React.useState(false)
  // The start's input schema while the run dialog is open.
  const [running, setRunning] = React.useState<Record<string, unknown>>()
  const [detailsWidth, setDetailsWidth] = useDetailsWidth(
    "forge.agents.detailsWidth"
  )
  const saving = useAgentAutosave(organization.id, record)
  const scoped = organizationAgents.scope({ organizationId: organization.id })
  const make = scoped.useCreate({
    meta: { errorTitle: "Couldn't duplicate the ADK workflow" },
  })
  const drop = scoped.useDelete({
    meta: { errorTitle: "Couldn't delete the ADK workflow" },
  })
  useUndoKeys()
  useLookups(organization.id)

  useShellPage({ header: { title: name || "Untitled ADK workflow" } })

  const canRun = useScopeAccess(`org:${organization.id}`)("agents:run")
  const errors = useAgentBuilder(
    (s) => s.issues.filter((i) => i.level === "error").length
  )
  // A run takes the ADK workflow as it's saved: only once what's shown is.
  const unsaved = saving.state.kind !== "saved"
  const runBlocked = errors
    ? `Fix the ${errors === 1 ? "problem" : `${errors} problems`} first: it wouldn't build with ${errors === 1 ? "it" : "them"}.`
    : unsaved
      ? "It runs as saved: wait until your changes are."
      : undefined
  const startRun = () => {
    const doc = agentDocumentOf(api.getState())
    const start = doc.nodes.find((node) => node.kind === "start")
    setRunning(start?.kind === "start" ? (start.config.input_schema ?? {}) : {})
  }
  const started = (run: AdkRun) =>
    toast.add({
      title: "Run started.",
      description:
        "It's queued, and a worker takes it within seconds. Follow it under Runs, or on its page.",
      type: "success",
      actionProps: {
        children: "Open the run",
        onClick: () =>
          void navigate({
            to: "/organizations/$organizationId",
            params: { organizationId: organization.id },
            search: { view: "agents", agentRun: run.id },
          }),
      },
    })

  const parse = React.useCallback(
    (text: string) => parseAgent(text, { organizationId: organization.id }),
    [organization.id]
  )
  const exportJson = () => {
    const doc = agentDocumentOf(api.getState())
    downloadJson(doc, agentFileName(doc))
  }
  const duplicate = () => {
    const doc = agentDocumentOf(api.getState())
    make.mutate(
      { document: copyAgent(doc, `${doc.name} (copy)`) },
      {
        onSuccess: (copy) =>
          void navigate({
            to: "/organizations/$organizationId/agents/$agentId",
            params: { organizationId: organization.id, agentId: copy.id },
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
          search: { view: "agents" },
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
        <AdkRunsMenu organizationId={organization.id} agentId={record.id} />
        <DropdownMenu>
          <DropdownMenuTrigger
            render={
              <Button
                variant="ghost"
                size="icon"
                aria-label="More ADK workflow actions"
              />
            }
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
              Duplicate ADK workflow
            </DropdownMenuItem>
            <DropdownMenuItem onClick={() => setImporting(true)}>
              <Icon icon="file" />
              Replace from JSON…
            </DropdownMenuItem>
            <DropdownMenuItem
              onClick={() =>
                downloadJson(AGENT_JSON_SCHEMA, "forge-agent.schema.json")
              }
            >
              <Icon icon="download" />
              Download the JSON Schema
            </DropdownMenuItem>
            <DropdownMenuSeparator />
            <DropdownMenuItem
              variant="destructive"
              onClick={() => setDeleting(true)}
            >
              Delete ADK workflow…
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
        {canRun &&
          (runBlocked ? (
            <Tooltip>
              <TooltipTrigger
                render={
                  <span tabIndex={0} className="rounded-(--radius-control)" />
                }
              >
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
        <AdkRunDialog
          open={running !== undefined}
          onOpenChange={(open) => !open && setRunning(undefined)}
          organizationId={organization.id}
          agentId={record.id}
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
        style={
          { "--details-width": `${detailsWidth}rem` } as React.CSSProperties
        }
      >
        <div
          className={cn(
            "relative min-h-0 min-w-0",
            view === "json" && "invisible"
          )}
          inert={view === "json" || undefined}
        >
          <BuilderCanvas needsLayout={needsLayout} />
        </div>
        <aside
          id={DETAILS_PANEL_ID}
          aria-label="ADK workflow details"
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
            <AgentDetails />
          </div>
          {/* Kept clear for the assistant's launcher, which floats here. */}
          <div aria-hidden="true" className="h-18 shrink-0" />
        </aside>
        {view === "json" && (
          <div className="absolute inset-0 bg-background">
            <BuilderJson<AgentDocument>
              format={AGENT_FORMAT}
              schema={AGENT_JSON_SCHEMA}
              schemaFileName="forge-agent.schema.json"
              fileName={agentFileName}
              about={
                <>
                  The Google ADK graph the admin API builds:{" "}
                  <code className="font-mono text-[0.9em]">nodes</code> are its
                  nodes, their sub-agents inside their{" "}
                  <code className="font-mono text-[0.9em]">config</code>, and{" "}
                  <code className="font-mono text-[0.9em]">edges</code> join a
                  node&apos;s way out (next, or a branch: success, a case,
                  approved…) to the next node.{" "}
                  <code className="font-mono text-[0.9em]">layout</code> is only
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
        format={AGENT_FORMAT}
        nouns={NOUNS}
        parse={parse}
        title="Replace from JSON"
        description="Its nodes and edges replace what's on the canvas; the name and description come with them. Undo puts the canvas back."
        action="Replace the canvas"
        onImport={(doc, layout) => {
          const store = api.getState()
          store.replaceDocument(doc)
          store.setView("canvas")
          if (layout) {
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
              It's removed for everyone in {organization.name}, with its nodes
              and edges. ADK workflows that run it as a saved workflow will need another.
              You can't undo this; export its JSON first to keep a copy.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <DialogClose render={<Button type="button" variant="outline" />}>
              Cancel
            </DialogClose>
            <Button
              variant="destructive"
              disabled={drop.isPending}
              onClick={remove}
            >
              {drop.isPending && <Spinner data-icon="inline-start" />}
              Delete ADK workflow
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  )
}
