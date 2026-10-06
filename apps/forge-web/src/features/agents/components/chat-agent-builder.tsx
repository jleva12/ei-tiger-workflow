import * as React from "react"
import { Link, useNavigate } from "@tanstack/react-router"
import { useQueryClient } from "@tanstack/react-query"
import { ReactFlowProvider, useReactFlow } from "@xyflow/react"
import { PackageIcon, PencilEdit02Icon } from "@hugeicons/core-free-icons"
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
import { BuilderUiContext } from "@/features/builder/components/ui"
import {
  DETAILS_PANEL_ID,
  downloadJson,
  measureFlow,
  useDetailsWidth,
  useUndoKeys,
} from "@/features/builder/components/utils"
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
import { useMyOrganizations, type MyOrganization } from "@/lib/access"
import {
  useOrganizationAgents,
  versionChoices,
} from "@/features/adk-workflows/lib/api"
import { toApiError } from "@/lib/api/index"
import {
  cacheChatAgent,
  conflictCode,
  exportChatAgentVersion,
  getChatAgent,
  publishProblems,
  saveChatAgent,
  useChatAgent,
  useChatAgentLifecycle,
  useChatAgentVersion,
  useDeleteChatAgent,
  useDuplicateChatAgent,
  useOrganizationChatAgents,
  type ChatAgentDetail,
  type ChatAgentRecord,
} from "@/features/agents/lib/api"
import { useScopeAccess } from "@/lib/hierarchy"
import {
  CHAT_AGENT_FORMAT,
  parseChatAgent,
  storedChatAgent,
  usesOf,
  type ChatAgentDocument,
} from "@/features/agents/lib/document"
import { CHAT_AGENTS_ICON } from "@/features/agents/lib/model"
import { CHAT_AGENT_JSON_SCHEMA } from "@/features/agents/lib/schema"
import { usePageContext } from "@/features/assistant/lib/page-context"
import { useAgentStepModels } from "@/features/steps/lib/models"
import { ChatAgentDetails } from "./chat-agent-details"
import { StandaloneDialog } from "./chat-agent-standalone"
import { ChatAgentTestPanel } from "./chat-agent-test"
import {
  PublishDialog,
  ReadOnlyBanner,
  RunInfo,
  VersionChip,
} from "./chat-agent-versions"
import { chatAgentFileName } from "./chat-agent-files"
import {
  ChatBuilderProvider,
  chatDocumentOf,
  useChatBuilder,
  useChatBuilderApi,
  type ChatEdgeView,
  type ChatNodeView,
} from "./chat-agent-store"
import { useToolLookups } from "./tool-lookups"
import { CHAT_AGENT_UI } from "./chat-agent-ui"

const NOUNS = { doc: "agent", steps: "nodes" }
const SCHEMA_FILE = "forge-chat-agent.schema.json"

/**
 * A chat agent's builder: the library of what can be attached in the
 * sidebar, the canvas with the agent and what it calls and hands off to,
 * the agent's details on the right, each node's settings in a dialog over
 * the canvas, and the agent's JSON a tab away. The draft is saved as it's
 * changed (the builder kit's autosave); a published version is read-only,
 * and changing it means starting a new version.
 */
export function ChatAgentBuilderPage({
  organizationId,
  agentId,
  version,
}: {
  organizationId: string
  agentId: string
  /** A published version to open read-only; the agent as it is now when omitted. */
  version?: number
}) {
  const myOrganizations = useMyOrganizations()
  const organization = myOrganizations.data?.find(
    (t) => t.id === organizationId
  )
  // Always the latest on opening: the builder saves from the revision it starts at.
  const agent = useChatAgent(organizationId, organization ? agentId : undefined)
  const pinned = useChatAgentVersion(organizationId, agentId, version)
  const can = useScopeAccess(organization ? `org:${organizationId}` : undefined)
  const canManage = can("agents:manage")
  // What the builder opened. From then on it owns the document: a later
  // refetch, even a failed one, never closes it (and what's unsaved with it).
  // Publishing, a new version or discarding a draft opens what they answer.
  const [record, setRecord] = React.useState<ChatAgentDetail>()
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
      title: agent.data?.document.name ?? "Agent",
      icon: CHAT_AGENTS_ICON,
      breadcrumbs: organization
        ? [
            {
              label: organization.name,
              href: `/organizations/${organizationId}`,
            },
            {
              label: "Agents",
              href: `/organizations/${organizationId}?view=chat-agents`,
            },
          ]
        : [],
    },
    // The builder draws its own sub nav (the node library), not the workspace's.
    sidebar: { label: "Agent nodes", sections: [] },
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
        description="Only an organization's members can open its agents. Pick one of your organizations at the top of the sidebar, or ask one of the organization's admins to add you."
      />
    )
  }
  if (!record && agent.error && toApiError(agent.error).status !== 404) {
    return (
      <ErrorCallout
        title="Couldn't open the agent"
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
        title="Agent not found"
        description={
          pinned.error
            ? `The agent has no version ${version}.`
            : "There's no agent here: it may have been deleted, or the link is to another organization's."
        }
      >
        <Button
          variant="outline"
          nativeButton={false}
          render={
            <Link
              to="/organizations/$organizationId"
              params={{ organizationId }}
              search={{ view: "chat-agents" }}
            />
          }
        >
          All agents
        </Button>
      </PageEmpty>
    )
  }
  // A published version, or the agent for someone who can't change it, is read-only.
  const readOnly = version !== undefined || !record.has_draft || !canManage
  return (
    <OpenChatAgent
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
 * The builder, open on a chat agent. The document is read as an import is:
 * a setting its kind has gained since it was kept is filled in, and one it
 * no longer has is dropped, so it's kept as the format stands now.
 */
function OpenChatAgent({
  organization,
  record,
  viewing,
  readOnly,
  canManage,
  onRecord,
}: {
  organization: MyOrganization
  record: ChatAgentDetail
  viewing: number | undefined
  readOnly: boolean
  canManage: boolean
  onRecord: (record: ChatAgentRecord) => void
}) {
  const [doc] = React.useState(() =>
    storedChatAgent(record.document, organization.id)
  )
  return (
    <ChatBuilderProvider
      key={record.id}
      initial={{ doc, context: { selfId: record.id }, readOnly }}
    >
      <BuilderUiContext.Provider value={CHAT_AGENT_UI}>
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
    </ChatBuilderProvider>
  )
}

/**
 * Names for the IDs nodes hold (other agents, workflows, models, MCP
 * servers, knowledge bases), what saved-agent, workflow, MCP and knowledge
 * base nodes may pick, and what's checked against them.
 */
function useLookups(organizationId: string) {
  const api = useChatBuilderApi()
  const { agents } = useOrganizationChatAgents(organizationId)
  const {
    agents: workflows,
    records: workflowRecords,
    isSuccess: workflowsKnown,
  } = useOrganizationAgents(organizationId)
  const { models, defaultModel } = useAgentStepModels()
  const tools = useToolLookups(organizationId)
  React.useEffect(() => {
    const state = api.getState()
    state.setLookups(tools.lookups)
    state.setContext(tools.checks)
  }, [api, tools])
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
  React.useEffect(() => {
    const state = api.getState()
    state.setLookups({
      workflows: Object.fromEntries(workflows.map((w) => [w.id, w.name])),
      workflowVersions: versionChoices(workflowRecords),
    })
    // Unknown until they load: a pick isn't called deleted meanwhile.
    state.setContext({
      workflows: workflowsKnown
        ? new Map(workflows.map((w) => [w.id, { name: w.name }]))
        : undefined,
    })
  }, [api, workflows, workflowRecords, workflowsKnown])
}

/** Keeping the chat agent's draft as it's built. */
function useChatAgentAutosave(organizationId: string, record: ChatAgentRecord) {
  const client = useQueryClient()
  const io = React.useMemo(
    () => ({
      save: (
        id: string,
        body: { document: ChatAgentDocument; revision: number },
        options?: { keepalive?: boolean }
      ) => saveChatAgent(organizationId, id, body, options),
      fetch: (id: string) => getChatAgent(organizationId, id),
      cache: (saved: ChatAgentRecord) => cacheChatAgent(client, saved),
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
  record: ChatAgentDetail
  viewing: number | undefined
  readOnly: boolean
  canManage: boolean
  onRecord: (record: ChatAgentRecord) => void
  needsLayout: boolean
}) {
  const api = useChatBuilderApi()
  const flow = useReactFlow<ChatNodeView, ChatEdgeView>()
  const navigate = useNavigate()
  const view = useChatBuilder((s) => s.view)
  const name = useChatBuilder((s) => s.meta.name)
  // The details sit beside the canvas (over it when narrow), only when asked for.
  const details = useChatBuilder((s) => s.details)
  const [importing, setImporting] = React.useState(false)
  const [deleting, setDeleting] = React.useState(false)
  const [publishing, setPublishing] = React.useState(false)
  const [discarding, setDiscarding] = React.useState(false)
  const [testing, setTesting] = React.useState(false)
  const [standalone, setStandalone] = React.useState(false)
  const [problems, setProblems] = React.useState<string[]>([])
  const errors = useChatBuilder(
    (s) => s.issues.filter((i) => i.level === "error").length
  )
  const [detailsWidth, setDetailsWidth] = useDetailsWidth(
    "forge.chat-agents.detailsWidth"
  )
  const saving = useChatAgentAutosave(organization.id, record)
  const copy = useDuplicateChatAgent(organization.id)
  const drop = useDeleteChatAgent(organization.id)
  const lifecycle = useChatAgentLifecycle(organization.id, record.id)
  const openVersion = (version?: number) =>
    void navigate({
      to: "/organizations/$organizationId/chat-agents/$chatAgentId",
      params: { organizationId: organization.id, chatAgentId: record.id },
      search: version ? { version } : {},
    })
  const failed = (title: string) => (caught: unknown) =>
    toast.add({ title, description: toApiError(caught).message, type: "error" })
  useUndoKeys()
  useLookups(organization.id)

  useShellPage({ header: { title: name || "Untitled agent" } })

  const parse = React.useCallback(
    (text: string) => parseChatAgent(text, { organizationId: organization.id }),
    [organization.id]
  )
  // What the builder shows: a published version exports with the saved
  // agents it uses bundled in, ready for forge-agent serve.
  const shownVersion =
    viewing ??
    (record.has_draft ? undefined : (record.published_version ?? undefined))
  const exportJson = () => {
    const doc = chatDocumentOf(api.getState())
    if (shownVersion === undefined) {
      downloadJson(doc, chatAgentFileName(doc))
      return
    }
    exportChatAgentVersion(organization.id, record.id, shownVersion).then(
      (bundle) =>
        downloadJson(
          bundle,
          chatAgentFileName(doc).replace(/\.json$/, `.v${shownVersion}.json`)
        ),
      failed("Couldn't export the agent")
    )
  }
  const duplicate = () => {
    copy.mutate(
      { id: record.id, name: `${name} (copy)` },
      {
        onSuccess: (made) =>
          void navigate({
            to: "/organizations/$organizationId/chat-agents/$chatAgentId",
            params: { organizationId: organization.id, chatAgentId: made.id },
          }),
        onError: failed("Couldn't duplicate the agent"),
      }
    )
  }
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
  const remove = () => {
    drop.mutate(api.getState().meta.id, {
      onSuccess: () => {
        toast.add({ title: `Deleted ${name}`, type: "success" })
        void navigate({
          to: "/organizations/$organizationId",
          params: { organizationId: organization.id },
          search: { view: "chat-agents" },
        })
      },
      onError: failed("Couldn't delete the agent"),
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
        <Button
          variant="outline"
          size="sm"
          aria-pressed={testing}
          onClick={() => setTesting(!testing)}
        >
          <Icon icon="message" data-icon="inline-start" />
          Test
        </Button>
        <Button variant="outline" size="sm" onClick={() => setStandalone(true)}>
          <Icon icon={PackageIcon} data-icon="inline-start" />
          Generate standalone agent
        </Button>
        {canManage && viewing === undefined && record.has_draft && (
          <Button
            size="sm"
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
                aria-label="More agent actions"
              />
            }
          >
            <Icon icon="more" />
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="w-56">
            {record.versions.length > 0 && (
              <>
                {record.has_draft && viewing !== undefined && (
                  <DropdownMenuItem onClick={() => openVersion()}>
                    <Icon icon={PencilEdit02Icon} />
                    Open the draft (v{record.draft_version})
                  </DropdownMenuItem>
                )}
                {record.versions.map((v) => (
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
              {shownVersion === undefined
                ? "Export JSON"
                : `Export v${shownVersion} to run anywhere`}
            </DropdownMenuItem>
            {canManage && (
              <DropdownMenuItem onClick={duplicate}>
                <Icon icon="copy" />
                Duplicate as a new agent
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
              onClick={() => downloadJson(CHAT_AGENT_JSON_SCHEMA, SCHEMA_FILE)}
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
                  Delete agent…
                </DropdownMenuItem>
              </>
            )}
          </DropdownMenuContent>
        </DropdownMenu>
      </ShellHeaderActions>

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
          aria-label="Agent details"
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
            <ChatAgentDetails>
              <RunInfo record={record} />
            </ChatAgentDetails>
          </div>
          {/* Kept clear for the assistant's launcher, which floats here. */}
          <div aria-hidden="true" className="h-18 shrink-0" />
        </aside>
        {view === "json" && (
          <div className="absolute inset-0 bg-background">
            <BuilderJson<ChatAgentDocument>
              format={CHAT_AGENT_FORMAT}
              schema={CHAT_AGENT_JSON_SCHEMA}
              schemaFileName={SCHEMA_FILE}
              fileName={chatAgentFileName}
              about={
                <>
                  One Google ADK chat agent:{" "}
                  <code className="font-mono text-[0.9em]">nodes</code> are the
                  agent, its sub-agents and its tools, and{" "}
                  <code className="font-mono text-[0.9em]">edges</code> join an
                  agent&apos;s way out (
                  <code className="font-mono text-[0.9em]">tools</code>, what it
                  can call, or{" "}
                  <code className="font-mono text-[0.9em]">agents</code>, who it
                  hands off to) to what&apos;s attached.{" "}
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

      {testing && (
        <ChatAgentTestPanel
          record={record}
          appName={
            viewing !== undefined
              ? `${record.id}@${viewing}`
              : record.has_draft
                ? `${record.id}@draft`
                : record.id
          }
          onClose={() => setTesting(false)}
        />
      )}

      <StandaloneDialog
        key={shownVersion ?? "draft"}
        open={standalone}
        onOpenChange={setStandalone}
        record={record}
        shownVersion={shownVersion}
      />

      <PublishDialog
        open={publishing}
        onOpenChange={setPublishing}
        record={record}
        name={name}
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
              Its changes are lost, and the agent is published v
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

      <ImportDialog
        open={importing}
        onOpenChange={setImporting}
        format={CHAT_AGENT_FORMAT}
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
              Every version stops running: apps calling {record.id} get a 404.
              Agents that use it as a saved agent will need another. You
              can&apos;t undo this; export its JSON first to keep a copy.
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
              Delete agent
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  )
}
