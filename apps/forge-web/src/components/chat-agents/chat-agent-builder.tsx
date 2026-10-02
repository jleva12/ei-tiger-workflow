import * as React from "react"
import { Link, useNavigate } from "@tanstack/react-router"
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
import { useMyOrganizations, type MyOrganization } from "@/lib/access"
import { useOrganizationAgents } from "@/lib/agents/api"
import { toApiError } from "@/lib/api"
import {
  cacheChatAgent,
  getChatAgent,
  saveChatAgent,
  useChatAgent,
  useCreateChatAgent,
  useDeleteChatAgent,
  useOrganizationChatAgents,
  useSaver,
  type ChatAgentRecord,
} from "@/lib/chat-agents/api"
import {
  CHAT_AGENT_FORMAT,
  copyChatAgent,
  parseChatAgent,
  storedChatAgent,
  usesOf,
  type ChatAgentDocument,
} from "@/lib/chat-agents/document"
import { CHAT_AGENTS_ICON } from "@/lib/chat-agents/model"
import { CHAT_AGENT_JSON_SCHEMA } from "@/lib/chat-agents/schema"
import { usePageContext } from "@/lib/page-context"
import { useAgentStepModels } from "@/lib/steps/models"
import { ChatAgentDetails } from "./chat-agent-details"
import { chatAgentFileName } from "./chat-agent-files"
import {
  ChatBuilderProvider,
  chatDocumentOf,
  useChatBuilder,
  useChatBuilderApi,
  type ChatEdgeView,
  type ChatNodeView,
} from "./chat-agent-store"
import { CHAT_AGENT_UI } from "./chat-agent-ui"

const NOUNS = { doc: "agent", steps: "nodes" }
const SCHEMA_FILE = "forge-chat-agent.schema.json"

/**
 * A chat agent's builder: the library of what can be attached in the
 * sidebar, the canvas with the agent and what it calls and hands off to,
 * the agent's details on the right, each node's settings in a dialog over
 * the canvas, and the agent's JSON a tab away. Changes are kept as they're
 * made (the builder kit's autosave), in this browser until the admin API
 * keeps agents.
 */
export function ChatAgentBuilderPage({
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
  const agent = useChatAgent(organizationId, organization ? agentId : undefined)
  // What the builder opened. From then on it owns the document: a later
  // refetch, even a failed one, never closes it (and what's unsaved with it).
  const [record, setRecord] = React.useState<ChatAgentRecord>()
  if (!record && agent.isFetchedAfterMount && agent.isSuccess)
    setRecord(agent.data)

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
        description="There's no agent here: it may have been deleted, or it was made in another browser (agents are kept in the browser that made them for now), or the link is to another organization's."
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
  return <OpenChatAgent organization={organization} record={record} />
}

/**
 * The builder, open on a chat agent. The document is read as an import is:
 * a setting its kind has gained since it was kept is filled in, and one it
 * no longer has is dropped, so it's kept as the format stands now.
 */
function OpenChatAgent({
  organization,
  record,
}: {
  organization: MyOrganization
  record: ChatAgentRecord
}) {
  const [doc] = React.useState(() =>
    storedChatAgent(record.document, organization.id)
  )
  return (
    <ChatBuilderProvider
      key={record.id}
      initial={{ doc, context: { selfId: record.id } }}
    >
      <BuilderUiContext.Provider value={CHAT_AGENT_UI}>
        <ReactFlowProvider>
          <Builder
            organization={organization}
            record={record}
            needsLayout={doc.nodes.some((n) => !doc.layout[n.id])}
          />
        </ReactFlowProvider>
      </BuilderUiContext.Provider>
    </ChatBuilderProvider>
  )
}

/**
 * Names for the IDs nodes hold (other agents, ADK workflows, models), what
 * saved-agent and ADK workflow nodes may pick, and what's checked against
 * them.
 */
function useLookups(organizationId: string) {
  const api = useChatBuilderApi()
  const { agents } = useOrganizationChatAgents(organizationId)
  const { agents: workflows, isSuccess: workflowsKnown } =
    useOrganizationAgents(organizationId)
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
  React.useEffect(() => {
    const state = api.getState()
    state.setLookups({
      workflows: Object.fromEntries(workflows.map((w) => [w.id, w.name])),
    })
    // Unknown until they load: a pick isn't called deleted meanwhile.
    state.setContext({
      workflows: workflowsKnown
        ? new Map(workflows.map((w) => [w.id, { name: w.name }]))
        : undefined,
    })
  }, [api, workflows, workflowsKnown])
}

/** Keeping the chat agent as it's built. */
function useChatAgentAutosave(organizationId: string, record: ChatAgentRecord) {
  const client = useQueryClient()
  const by = useSaver()
  const io = React.useMemo(
    () => ({
      save: (
        id: string,
        body: { document: ChatAgentDocument; revision: number }
      ) => saveChatAgent(organizationId, id, body, by),
      fetch: (id: string) => getChatAgent(organizationId, id),
      cache: (saved: ChatAgentRecord) => cacheChatAgent(client, saved),
    }),
    [organizationId, client, by]
  )
  return useBuilderAutosave(record, io)
}

function Builder({
  organization,
  record,
  needsLayout,
}: {
  organization: MyOrganization
  record: ChatAgentRecord
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
  const [detailsWidth, setDetailsWidth] = useDetailsWidth(
    "forge.chat-agents.detailsWidth"
  )
  const saving = useChatAgentAutosave(organization.id, record)
  const make = useCreateChatAgent(organization.id)
  const drop = useDeleteChatAgent(organization.id)
  const failed = (title: string) => (caught: unknown) =>
    toast.add({ title, description: toApiError(caught).message, type: "error" })
  useUndoKeys()
  useLookups(organization.id)

  useShellPage({ header: { title: name || "Untitled agent" } })

  const parse = React.useCallback(
    (text: string) => parseChatAgent(text, { organizationId: organization.id }),
    [organization.id]
  )
  const exportJson = () => {
    const doc = chatDocumentOf(api.getState())
    downloadJson(doc, chatAgentFileName(doc))
  }
  const duplicate = () => {
    const doc = chatDocumentOf(api.getState())
    make.mutate(copyChatAgent(doc, `${doc.name} (copy)`), {
      onSuccess: (copy) =>
        void navigate({
          to: "/organizations/$organizationId/chat-agents/$chatAgentId",
          params: { organizationId: organization.id, chatAgentId: copy.id },
        }),
      onError: failed("Couldn't duplicate the agent"),
    })
  }
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
        <SaveStatus saving={saving} organizationName={organization.name} />
        <IssuesButton />
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
            <DropdownMenuItem onClick={exportJson}>
              <Icon icon="download" />
              Export JSON
            </DropdownMenuItem>
            <DropdownMenuItem onClick={duplicate}>
              <Icon icon="copy" />
              Duplicate agent
            </DropdownMenuItem>
            <DropdownMenuItem onClick={() => setImporting(true)}>
              <Icon icon="file" />
              Replace from JSON…
            </DropdownMenuItem>
            <DropdownMenuItem
              onClick={() => downloadJson(CHAT_AGENT_JSON_SCHEMA, SCHEMA_FILE)}
            >
              <Icon icon="download" />
              Download the JSON Schema
            </DropdownMenuItem>
            <DropdownMenuSeparator />
            <DropdownMenuItem
              variant="destructive"
              onClick={() => setDeleting(true)}
            >
              Delete agent…
            </DropdownMenuItem>
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
            <ChatAgentDetails />
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
