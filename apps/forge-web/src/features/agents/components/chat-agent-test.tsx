import * as React from "react"

import {
  AssistantProvider,
  AssistantThread,
  knowledgeBaseSources,
  useAdkAssistant,
} from "@/components/forge/assistant/index"
import { Icon } from "@/components/forge/icon"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { consoleHeaders } from "@/features/agents/lib/runtime-headers"
import { useMe } from "@/lib/users"
import {
  RUNTIME_URL,
  useOrganizationChatAgents,
  type ChatAgentRecord,
} from "@/features/agents/lib/api"
import {
  agentNamesOf,
  usesOf,
  workflowNamesOf,
} from "@/features/agents/lib/document"
import type { ModelWindow } from "@/components/forge/assistant/context-window"
import { declaredState } from "@/features/agents/lib/input"
import { useTestPanelSize } from "@/features/agents/lib/test-panel-size"
import {
  testGraphOf,
  useTestProgress,
  type TestGraph,
} from "@/features/agents/lib/test-progress"
import { WorkflowRunsContext } from "@/features/agents/lib/workflow-run-context"
import { workflowRunToolkit } from "@/features/agents/lib/workflow-run-toolkit"
import { workflowRunFollower } from "@/features/agents/lib/workflow-runs"
import type { RunOverlay } from "@/features/builder/components/run-overlay"
import { knowledgeDocumentHref } from "@/features/knowledge/lib/sources"
import { useOrganizationMcpServers } from "@/features/mcp-servers/lib/api"
import { findModel, useAgentStepModels } from "@/features/steps/lib/models"
import type { DataType } from "@/features/steps/lib/types"

import { TestPanelResizer } from "./test-panel-resizer"

/**
 * Talking to the agent as a chat would: the runtime's run API, the version
 * the builder shows (`ca_x@draft`, `ca_x@3`, or `ca_x`), as the signed-in
 * person. The state the agent declares is filled in here and sent with
 * every message, as the chat's `stateDelta`. Each turn is drawn over the
 * canvas as it goes (`onProgress`): the agent answering and the tools it
 * calls, from the runtime's events.
 */
export function ChatAgentTestPanel({
  record,
  appName,
  onClose,
  onProgress,
}: {
  record: ChatAgentRecord
  appName: string
  onClose: () => void
  /** What the canvas draws of the chat: null for nothing. */
  onProgress: (overlay: RunOverlay | null) => void
}) {
  const me = useMe()
  // The runtime keeps a person's conversations under their own ID only.
  const userId = me.data?.subject
  const { fields } = React.useMemo(() => {
    const entry = record.document.nodes.find((n) => n.kind === "agent")
    return declaredState(
      entry?.kind === "agent" ? entry.config.state_schema : {}
    )
  }, [record.document])
  // The model its agent starts on (the runtime's default for one it doesn't
  // offer): the context meter shows its window from the start.
  const stepModels = useAgentStepModels()
  const contextModel = React.useMemo((): ModelWindow | undefined => {
    const entry = record.document.nodes.find((n) => n.kind === "agent")
    const choice = entry?.kind === "agent" ? entry.config.model : undefined
    const listed = stepModels.models
    return (
      (choice && findModel(listed, choice)) ??
      listed.find((model) => model.id === stepModels.defaultModel) ??
      listed[0]
    )
  }, [record.document, stepModels.models, stepModels.defaultModel])
  // Its agents' names, so a hand-off to one shows as asking it.
  const agents = React.useMemo(
    () => agentNamesOf(record.document),
    [record.document]
  )
  // Its workflow tools, so a call of one shows as the run it starts.
  const workflows = React.useMemo(
    () => workflowNamesOf(record.document),
    [record.document]
  )
  const graph = useTestGraph(record)
  const [values, setValues] = React.useState<Record<string, string>>({})
  // What the next message sends, read as it's sent.
  const latest = React.useRef(values)
  React.useEffect(() => {
    latest.current = values
  })
  // Full height beside the canvas until its corner is dragged.
  const panel = React.useRef<HTMLElement>(null)
  const [size, setSize] = useTestPanelSize("forge.chat-agents.testSize")

  return (
    <aside
      ref={panel}
      aria-label={`Test ${record.document.name}`}
      style={
        size
          ? {
              top: "auto",
              width: `${size.width}rem`,
              height: `${size.height}rem`,
            }
          : undefined
      }
      className="group/test-panel fixed top-16 right-4 bottom-4 z-30 flex max-h-[calc(100vh-5rem)] w-[min(28rem,calc(100vw-2rem))] max-w-[calc(100vw-2rem)] flex-col overflow-hidden rounded-(--radius-card) border bg-background shadow-(--shadow-float)"
    >
      <TestPanelResizer panel={panel} onSizeChange={setSize} />
      <header className="flex items-start gap-3 border-b px-4 pt-3 pb-2.5">
        <div className="flex min-w-0 flex-1 flex-col">
          <h2 className="truncate text-sm font-medium text-foreground">
            Test {record.document.name || "the agent"}
          </h2>
          <p className="truncate text-2xs text-muted-foreground">
            As {userId ?? "you"}, through the runtime:{" "}
            <code className="font-mono">appName {appName}</code>
          </p>
        </div>
        <Button
          variant="ghost"
          size="icon-sm"
          aria-label="Close the test chat"
          onClick={onClose}
          className="-mr-1.5 text-muted-foreground"
        >
          <Icon icon="close" />
        </Button>
      </header>
      {fields.length > 0 && (
        <section className="flex flex-col gap-2 border-b px-4 py-2.5">
          <p className="text-2xs text-muted-foreground">
            State sent with each message (stateDelta)
          </p>
          {fields.map(([name, type]) => (
            <label key={name} className="flex items-center gap-2 text-xs">
              <code className="w-32 shrink-0 truncate font-mono text-foreground">
                {name}
              </code>
              <StateInput
                type={type}
                value={values[name] ?? ""}
                onChange={(value) => setValues({ ...values, [name]: value })}
              />
            </label>
          ))}
        </section>
      )}
      <div className="min-h-0 flex-1">
        {RUNTIME_URL && userId ? (
          <TestChat
            key={`${appName}:${userId}`}
            appName={appName}
            userId={userId}
            organizationId={record.organization_id}
            name={record.document.name}
            agents={agents}
            workflows={workflows}
            state={() => stateDelta(fields, latest.current)}
            graph={graph}
            onProgress={onProgress}
            contextModel={contextModel}
          />
        ) : (
          <p className="p-4 text-xs text-muted-foreground">
            {RUNTIME_URL
              ? me.error
                ? "Couldn't tell who you are, which your conversations are kept under."
                : "Finding who you are…"
              : "Set VITE_API_URL to talk to agents through the runtime."}
          </p>
        )}
      </div>
    </aside>
  )
}

function TestChat({
  appName,
  userId,
  organizationId,
  name,
  agents,
  workflows,
  state,
  graph,
  onProgress,
  contextModel,
}: {
  appName: string
  userId: string
  organizationId: string
  name: string
  /** Its agents by ADK name, as the canvas names them. */
  agents: Record<string, string>
  /** Its workflow tools by ADK name, as the canvas names them. */
  workflows: Record<string, string>
  state: () => Record<string, unknown>
  /** Its canvas, as the runtime's events name its agents and tools. */
  graph: TestGraph
  onProgress: (overlay: RunOverlay | null) => void
  /** The model its agent starts on, for the context meter. */
  contextModel: ModelWindow | undefined
}) {
  // Each turn, followed over the canvas.
  const progress = useTestProgress(graph, onProgress)
  // Its knowledge base tools' passages, cited in its answers; each opens in
  // its knowledge base's document viewer, in the agent's organization.
  const sources = React.useMemo(
    () =>
      knowledgeBaseSources({
        documentHref: (passage) =>
          knowledgeDocumentHref(passage, organizationId),
      }),
    [organizationId]
  )
  const { runtime, artifacts } = useAdkAssistant({
    requireTurnOutcome: true,
    adk: { url: RUNTIME_URL!, appName, userId },
    // Who's testing: needed when the runtime isn't public, and their turns
    // are theirs when it is. The console header has the audit list them as
    // the builder's chat, not an outside app's.
    headers: consoleHeaders,
    runState: state,
    // As a chat would show them: a few questions to ask next, after each reply.
    suggestFollowUps: true,
    onEvent: progress.onEvent,
  })
  const { follow } = progress
  React.useEffect(() => follow(runtime), [follow, runtime])
  // Its workflow tools' calls, each drawn as the run it starts; the run's
  // page opens from the card.
  const follower = React.useMemo(
    () =>
      workflowRunFollower({
        url: RUNTIME_URL!,
        appName,
        userId,
        headers: consoleHeaders,
        names: workflows,
        runHref: (runId) =>
          `/organizations/${encodeURIComponent(organizationId)}?view=agents&agentRun=${encodeURIComponent(runId)}`,
      }),
    [appName, userId, workflows, organizationId]
  )
  const toolkit = React.useMemo(
    () => workflowRunToolkit(workflows),
    [workflows]
  )
  return (
    <WorkflowRunsContext.Provider value={follower}>
      <AssistantProvider
        runtime={runtime}
        toolkit={toolkit}
        artifacts={artifacts}
        sources={sources}
        title={name || "Agent"}
        agents={agents}
        contextModel={contextModel}
        welcome={{
          title: `Talk to ${name || "the agent"}`,
          description:
            "Each message runs it as a chat would, with the state above.",
        }}
      >
        <AssistantThread />
      </AssistantProvider>
    </WorkflowRunsContext.Provider>
  )
}

/**
 * Its canvas as a test chat's events name it: its saved agents' own agents
 * and its MCP servers' tools come from the organization's, once they load.
 */
function useTestGraph(record: ChatAgentRecord): TestGraph {
  const saves = usesOf(record.document).length > 0
  const others = useOrganizationChatAgents(record.organization_id, {
    enabled: saves,
  })
  const servers = useOrganizationMcpServers(record.organization_id)
  return React.useMemo(
    () =>
      testGraphOf(record.document, {
        savedAgent: (id) => others.records.find((r) => r.id === id)?.document,
        mcpTools: (id) => {
          const server = servers.data?.find((s) => s.id === id)
          return server?.checked_at ? server.tools.map((t) => t.name) : null
        },
      }),
    [record.document, others.records, servers.data]
  )
}

function StateInput({
  type,
  value,
  onChange,
}: {
  type: DataType
  value: string
  onChange: (value: string) => void
}) {
  if (type.kind === "string" && type.enum?.length) {
    return (
      <select
        className="h-7 min-w-0 flex-1 rounded-(--radius-control) border bg-background px-2 text-xs"
        value={value}
        onChange={(event) => onChange(event.target.value)}
      >
        <option value="">Not sent</option>
        {type.enum.map((option) => (
          <option key={option} value={option}>
            {option}
          </option>
        ))}
      </select>
    )
  }
  if (type.kind === "boolean") {
    return (
      <select
        className="h-7 min-w-0 flex-1 rounded-(--radius-control) border bg-background px-2 text-xs"
        value={value}
        onChange={(event) => onChange(event.target.value)}
      >
        <option value="">Not sent</option>
        <option value="true">true</option>
        <option value="false">false</option>
      </select>
    )
  }
  return (
    <Input
      className="h-7 min-w-0 flex-1 text-xs"
      value={value}
      placeholder={
        type.kind === "object" || type.kind === "array" ? "JSON" : "Not sent"
      }
      onChange={(event) => onChange(event.target.value)}
    />
  )
}

/** The filled-in fields, as their types: what the chat sends in stateDelta. */
function stateDelta(
  fields: [string, DataType][],
  values: Record<string, string>
): Record<string, unknown> {
  const out: Record<string, unknown> = {}
  for (const [name, type] of fields) {
    const raw = values[name]
    if (raw === undefined || raw === "") continue
    switch (type.kind) {
      case "number":
        if (!Number.isNaN(Number(raw))) out[name] = Number(raw)
        break
      case "boolean":
        out[name] = raw === "true"
        break
      case "object":
      case "array":
      case "unknown":
        try {
          out[name] = JSON.parse(raw)
        } catch {
          out[name] = raw
        }
        break
      default:
        out[name] = raw
    }
  }
  return out
}
