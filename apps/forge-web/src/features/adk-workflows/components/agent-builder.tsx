import * as React from "react"
import { Link, useNavigate } from "@tanstack/react-router"
import { PlayIcon } from "@hugeicons/core-free-icons"
import { useQueryClient } from "@tanstack/react-query"
import { ReactFlowProvider, useReactFlow } from "@xyflow/react"
import { PencilEdit02Icon } from "@hugeicons/core-free-icons"
import { cn } from "cn"

import { useBuilderAutosave } from "@/features/builder/components/autosave"
import { BuilderCanvas } from "@/features/builder/components/canvas"
import {
  BuilderToolbar,
  IssuesButton,
  SaveStatus,
} from "@/features/builder/components/chrome"
import { DetailsResizer } from "@/features/builder/components/details-resizer"
import { ImportDialog } from "@/features/builder/components/import-dialog"
import { BuilderJson } from "@/features/builder/components/json-view"
import { StepLibrary } from "@/features/builder/components/library"
import { StepDialog } from "@/features/builder/components/step-dialog"
import {
  PublishDialog,
  ReadOnlyBanner,
  VersionChip,
} from "@/features/builder/components/versions"
import { BuilderUiContext } from "@/features/builder/components/ui"
import {
  DETAILS_PANEL_ID,
  downloadJson,
  measureFlow,
  useDetailsWidth,
  useUndoKeys,
} from "@/features/builder/components/utils"
import { PrimaryAction } from "@/components/forge/app-shell"
import { PageEmpty } from "@/components/forge/empty-state"
import { ErrorCallout } from "@/components/forge/feedback"
import { Icon } from "@/components/forge/icon"
import {
  ShellHeaderActions,
  ShellToolbar,
  useShellPage,
} from "@/components/forge/shell/index"
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
  useAgentLifecycle,
  useAgentVersion,
  useOrganizationAgents,
  versionChoices,
  type AgentDetail,
  type AgentRecord,
} from "@/features/adk-workflows/lib/api"
import {
  AGENT_FORMAT,
  copyAgent,
  parseAgent,
  storedAgent,
  usesOf,
  type AgentDocument,
} from "@/features/adk-workflows/lib/document"
import { AGENTS_ICON } from "@/features/adk-workflows/lib/model"
import { useToolLookups } from "@/features/agents/components/tool-lookups"
import {
  conflictCode,
  publishProblems,
  useOrganizationChatAgents,
} from "@/features/agents/lib/api"
import type { AdkRun } from "@/features/runs/lib/runs"
import { AGENT_JSON_SCHEMA } from "@/features/adk-workflows/lib/schema"
import { toApiError } from "@/lib/api/index"
import { useScopeAccess } from "@/lib/hierarchy"
import { usePageContext } from "@/features/assistant/lib/page-context"
import { useAgentStepModels } from "@/features/steps/lib/models"
import { AdkRunDialog, AdkRunsMenu } from "@/features/runs/components/adk-runs"
import { AgentDetails } from "./agent-details"
import { WorkflowRunInfo } from "./workflow-versions"
import { agentFileName } from "./agent-files"
import {
  AgentBuilderProvider,
  agentDocumentOf,
  useAgentBuilder,
  useAgentBuilderApi,
  type AgentEdgeView,
  type AgentNodeView,
  type ChatAgentChoice,
} from "./agent-store"
import { AGENT_UI } from "./agent-ui"

const NOUNS = { doc: "Workflow", steps: "nodes" }

/**
 * An agent's builder, for the organization's members: the node library in
 * the sidebar, the canvas, the agent's details on the right, each node's
 * settings (and its sub-agents') in a dialog over the canvas, and the
 * agent's JSON a tab away. The draft is saved to the organization as it's
 * changed (the builder kit's autosave); a published version is read-only,
 * and changing it means starting a new version.
 */
export function AgentBuilderPage({
  organizationId,
  agentId,
  version,
}: {
  organizationId: string
  agentId: string
  /** A published version to open read-only; the workflow as it is now when omitted. */
  version?: number
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
  const pinned = useAgentVersion(organizationId, agentId, version)
  const can = useScopeAccess(organization ? `org:${organizationId}` : undefined)
  const canManage = can("agents:manage")
  // What the builder opened. From then on it owns the document: a later
  // refetch, even a failed one, never closes it (and what's unsaved with it).
  // Publishing, a new version or discarding a draft opens what they answer.
  const [record, setRecord] = React.useState<AgentDetail>()
  if (
    !record &&
    agent.isFetchedAfterMount &&
    agent.isSuccess &&
    (version === undefined || pinned.isSuccess)
  ) {
    setRecord(
      pinned.data
        ? { ...agent.data, document: pinned.data.document }
        : agent.data
    )
  }

  useShellPage({
    header: {
      title: agent.data?.document.name ?? "Workflow",
      icon: AGENTS_ICON,
      breadcrumbs: organization
        ? [
            {
              label: organization.name,
              href: `/organizations/${organizationId}`,
            },
            {
              label: "Workflows",
              href: `/organizations/${organizationId}?view=agents`,
            },
          ]
        : [],
    },
    // The builder draws its own sub nav (the node library), not the workspace's.
    sidebar: { label: "Workflow nodes", sections: [] },
  })
  usePageContext({
    entities: [
      { kind: "organization", id: organizationId, label: organization?.name },
    ],
  })

  if (
    myOrganizations.isPending ||
    (organization && !record && !agent.error && !pinned.error)
  ) {
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
        description="Only an organization's members can open its workflows. Pick one of your organizations at the top of the sidebar, or ask one of the organization's admins to add you."
      />
    )
  }
  if (!record && agent.error && toApiError(agent.error).status !== 404) {
    return (
      <ErrorCallout
        title="Couldn't open the workflow"
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
        title="Workflow not found"
        description={
          pinned.error
            ? `The workflow has no version ${version}.`
            : "The organization has no workflow here: it may have been deleted, or the link is to another organization's."
        }
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
          All workflows
        </Button>
      </PageEmpty>
    )
  }
  // A published version, or the workflow for someone who can't change it, is read-only.
  const readOnly = version !== undefined || !record.has_draft || !canManage
  return (
    <OpenAgent
      key={`${version ?? (record.has_draft ? `draft${record.draft_version}` : `v${record.published_version}`)}:${readOnly}`}
      organization={organization}
      record={record}
      viewing={version}
      readOnly={readOnly}
      canManage={canManage}
      onRecord={(next) =>
        setRecord((kept) => ({ versions: kept?.versions ?? [], ...next }))
      }
    />
  )
}

/**
 * The builder, open on an agent. The document is read as an import is: a
 * setting its kind has gained since it was saved is filled in, and one it
 * no longer has is dropped, so it saves as the format stands now.
 */
function OpenAgent({
  organization,
  record,
  viewing,
  readOnly,
  canManage,
  onRecord,
}: {
  organization: MyOrganization
  record: AgentDetail
  viewing: number | undefined
  readOnly: boolean
  canManage: boolean
  onRecord: (record: AgentRecord) => void
}) {
  const [doc] = React.useState(() =>
    storedAgent(record.document, organization.id)
  )
  return (
    <AgentBuilderProvider
      key={record.id}
      initial={{ doc, context: { selfId: record.id }, readOnly }}
    >
      <BuilderUiContext.Provider value={AGENT_UI}>
        <ReactFlowProvider>
          <Builder
            organization={organization}
            record={record}
            viewing={viewing}
            readOnly={readOnly}
            canManage={canManage}
            onRecord={onRecord}
            needsLayout={doc.nodes.some((n) => !doc.layout[n.id])}
          />
        </ReactFlowProvider>
      </BuilderUiContext.Provider>
    </AgentBuilderProvider>
  )
}

/**
 * Names for the IDs nodes hold, what saved-agent nodes and LLM agents (and
 * their tools) may pick, and what's checked against them.
 */
function useLookups(organizationId: string) {
  const api = useAgentBuilderApi()
  const { agents, records } = useOrganizationAgents(organizationId)
  const { models, defaultModel } = useAgentStepModels()
  const chatAgents = useOrganizationChatAgents(organizationId)
  const tools = useToolLookups(organizationId)
  React.useEffect(() => {
    const state = api.getState()
    state.setLookups(tools.lookups)
    state.setContext(tools.checks)
  }, [api, tools])
  React.useEffect(() => {
    const state = api.getState()
    const choices = Object.fromEntries(
      chatAgents.records.map((r): [string, ChatAgentChoice] => {
        const entry = r.document.nodes.find((n) => n.kind === "agent")
        const input =
          entry?.kind === "agent" ? entry.config.state_schema : undefined
        return [
          r.id,
          {
            name: r.document.name || r.id,
            published: r.published_version,
            hasDraft: r.has_draft,
            draftVersion: r.draft_version,
            input: input ?? {},
          },
        ]
      })
    )
    state.setLookups({ chatAgents: choices })
    // Unknown until they load: a pick isn't called deleted meanwhile.
    state.setContext({
      chatAgents: chatAgents.isSuccess
        ? new Map(Object.entries(choices))
        : undefined,
    })
  }, [api, chatAgents.records, chatAgents.isSuccess])
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
      workflowVersions: versionChoices(records),
    })
    state.setContext({
      selfId: state.meta.id,
      agents: new Map(
        agents.map((a) => [a.id, { name: a.name, uses: usesOf(a) }])
      ),
    })
  }, [api, agents, records])
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
  viewing,
  readOnly,
  canManage,
  onRecord,
  needsLayout,
}: {
  organization: MyOrganization
  record: AgentDetail
  viewing: number | undefined
  readOnly: boolean
  canManage: boolean
  onRecord: (record: AgentRecord) => void
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
  const [publishing, setPublishing] = React.useState(false)
  const [discarding, setDiscarding] = React.useState(false)
  const [problems, setProblems] = React.useState<string[]>([])
  // The start's input schema while the run dialog is open.
  const [running, setRunning] = React.useState<Record<string, unknown>>()
  const [detailsWidth, setDetailsWidth] = useDetailsWidth(
    "forge.agents.detailsWidth"
  )
  const saving = useAgentAutosave(organization.id, record)
  const scoped = organizationAgents.scope({ organizationId: organization.id })
  const make = scoped.useCreate({
    meta: { errorTitle: "Couldn't duplicate the workflow" },
  })
  const drop = scoped.useDelete({
    meta: { errorTitle: "Couldn't delete the workflow" },
  })
  const lifecycle = useAgentLifecycle(organization.id, record.id)
  useUndoKeys()
  useLookups(organization.id)

  useShellPage({ header: { title: name || "Untitled workflow" } })

  const canRun = useScopeAccess(`org:${organization.id}`)("agents:run")
  const errors = useAgentBuilder(
    (s) => s.issues.filter((i) => i.level === "error").length
  )
  // What's shown: a published version (viewed, or the latest with no draft), or the draft.
  const shownVersion =
    viewing ??
    (record.has_draft ? undefined : (record.published_version ?? undefined))
  const runVersion = shownVersion ?? "draft"
  const openVersion = (version?: number) =>
    void navigate({
      to: "/organizations/$organizationId/agents/$agentId",
      params: { organizationId: organization.id, agentId: record.id },
      search: version ? { version } : {},
    })
  const failed = (title: string) => (caught: unknown) =>
    toast.add({ title, description: toApiError(caught).message, type: "error" })
  const publish = async () => {
    setProblems([])
    // Everything unsaved is saved first: what's published is what's on the canvas.
    const revision = await saving.flush()
    if (revision === null) {
      setProblems([
        "The latest changes aren't saved yet; publish once they are.",
      ])
      return
    }
    lifecycle.publish.mutate(revision, {
      onSuccess: (published) => {
        setPublishing(false)
        toast.add({
          title: `Published ${name} v${published.published_version}`,
          description: `${published.id} now runs it.`,
          type: "success",
        })
        onRecord(published)
      },
      onError: (caught) => {
        const found = publishProblems(caught)
        setProblems(found.length ? found : [toApiError(caught).message])
      },
    })
  }
  const newVersion = () =>
    lifecycle.newVersion.mutate(viewing, {
      onSuccess: (drafted) => {
        if (viewing !== undefined) openVersion()
        onRecord(drafted)
      },
      onError: (caught) =>
        conflictCode(caught) === "DRAFT_EXISTS"
          ? openVersion()
          : failed("Couldn't start a new version")(caught),
    })
  const discard = () =>
    lifecycle.discard.mutate(undefined, {
      onSuccess: (kept) => {
        setDiscarding(false)
        onRecord(kept)
      },
      onError: failed("Couldn't discard the draft"),
    })
  // A run of the draft takes it as it's saved: only once what's shown is.
  const unsaved = runVersion === "draft" && saving.state.kind !== "saved"
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
        <VersionChip record={record} viewing={viewing} />
        {!readOnly && (
          <SaveStatus saving={saving} organizationName={organization.name} />
        )}
        <IssuesButton />
        <AdkRunsMenu organizationId={organization.id} agentId={record.id} />
        {canManage && viewing === undefined && record.has_draft && (
          <Button
            size="sm"
            variant="outline"
            onClick={() => {
              setProblems([])
              setPublishing(true)
            }}
          >
            Publish v{record.draft_version}
          </Button>
        )}
        <DropdownMenu>
          <DropdownMenuTrigger
            render={
              <Button
                variant="ghost"
                size="icon"
                aria-label="More workflow actions"
              />
            }
          >
            <Icon icon="more" />
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="w-56">
            {(record.versions?.length ?? 0) > 0 && (
              <>
                {record.has_draft && viewing !== undefined && (
                  <DropdownMenuItem onClick={() => openVersion()}>
                    <Icon icon={PencilEdit02Icon} />
                    Open the draft (v{record.draft_version})
                  </DropdownMenuItem>
                )}
                {record.versions?.map((v) => (
                  <DropdownMenuItem
                    key={v.version}
                    disabled={v.version === shownVersion}
                    onClick={() =>
                      openVersion(
                        !record.has_draft &&
                          v.version === record.published_version
                          ? undefined
                          : v.version
                      )
                    }
                  >
                    <Icon icon="clock" />
                    Version {v.version}
                    {v.version === record.published_version ? " (latest)" : ""}
                  </DropdownMenuItem>
                ))}
                <DropdownMenuSeparator />
              </>
            )}
            <DropdownMenuItem onClick={exportJson}>
              <Icon icon="download" />
              Export JSON
            </DropdownMenuItem>
            {canManage && (
              <DropdownMenuItem onClick={duplicate}>
                <Icon icon="copy" />
                Duplicate workflow
              </DropdownMenuItem>
            )}
            {!readOnly && (
              <DropdownMenuItem onClick={() => setImporting(true)}>
                <Icon icon="file" />
                Replace from JSON…
              </DropdownMenuItem>
            )}
            {canManage &&
              record.has_draft &&
              record.published_version !== null && (
                <DropdownMenuItem onClick={() => setDiscarding(true)}>
                  <Icon icon="close" />
                  Discard draft v{record.draft_version}…
                </DropdownMenuItem>
              )}
            <DropdownMenuItem
              onClick={() =>
                downloadJson(AGENT_JSON_SCHEMA, "forge-agent.schema.json")
              }
            >
              <Icon icon="download" />
              Download the JSON Schema
            </DropdownMenuItem>
            {canManage && (
              <>
                <DropdownMenuSeparator />
                <DropdownMenuItem
                  variant="destructive"
                  onClick={() => setDeleting(true)}
                >
                  Delete workflow…
                </DropdownMenuItem>
              </>
            )}
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
          version={runVersion}
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
          {readOnly && shownVersion !== undefined && (
            <ReadOnlyBanner
              version={shownVersion}
              latest={record.published_version}
              canManage={canManage}
              hasDraft={record.has_draft}
              pending={lifecycle.newVersion.isPending}
              onNewVersion={newVersion}
              onOpenCurrent={() => openVersion()}
            />
          )}
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
            <WorkflowRunInfo record={record} viewing={viewing} />
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

      <PublishDialog
        open={publishing}
        onOpenChange={setPublishing}
        record={record}
        name={name}
        noun="workflow"
        errors={errors}
        problems={problems}
        pending={lifecycle.publish.isPending}
        onPublish={() => void publish()}
      />

      <Dialog open={discarding} onOpenChange={setDiscarding}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Discard draft v{record.draft_version}?</DialogTitle>
            <DialogDescription>
              Its changes are lost, and the workflow is version{" "}
              {record.published_version} again. You can&apos;t undo this.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <DialogClose render={<Button type="button" variant="outline" />}>
              Cancel
            </DialogClose>
            <Button
              variant="destructive"
              disabled={lifecycle.discard.isPending}
              onClick={discard}
            >
              {lifecycle.discard.isPending && (
                <Spinner data-icon="inline-start" />
              )}
              Discard draft
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={deleting} onOpenChange={setDeleting}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete {name}?</DialogTitle>
            <DialogDescription>
              It's removed for everyone in {organization.name}, with its nodes
              and edges. Workflows that run it as a saved workflow will need
              another. You can't undo this; export its JSON first to keep a
              copy.
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
              Delete workflow
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  )
}
