import * as React from "react"
import {
  ChatGptIcon,
  ClaudeIcon,
  GoogleGeminiIcon,
} from "@hugeicons/core-free-icons"

import {
  AssistantScreen,
  extendedThinkingLevels,
  useAdkAssistant,
  type AssistantModel,
} from "@/components/forge/assistant"
import {
  CanvasPanel,
  ChatHistoryPalette,
  NotesButton,
  NotesPanel,
  PlanButton,
  PlanPanel,
  ProgressDock,
  ToolsMenu,
  approvalViews,
  toolUIs,
  workspaceCommands,
  type AgentTool,
} from "@/components/forge/agent-workspace"

import { workspaceMockStream, workspaceSources } from "../agent-workspace-mock"
import { SectionPage, Specimen } from "../specimen"

// Grouped by maker, each with the thinking levels it offers.
const models: AssistantModel[] = [
  {
    id: "google/gemini-2.5-pro",
    name: "Gemini 2.5 Pro",
    description: "1M context",
    group: "Google",
    icon: GoogleGeminiIcon,
    thinkingLevels: ["low", "medium", "high"],
  },
  {
    id: "google/gemini-2.5-flash",
    name: "Gemini 2.5 Flash",
    description: "1M context",
    group: "Google",
    icon: GoogleGeminiIcon,
    thinkingLevels: ["off", "low", "medium", "high"],
  },
  {
    id: "anthropic/claude-sonnet",
    name: "Claude Sonnet",
    description: "200K context",
    group: "Anthropic",
    icon: ClaudeIcon,
  },
  {
    id: "openai/gpt",
    name: "GPT",
    description: "400K context",
    group: "OpenAI",
    icon: ChatGptIcon,
    thinkingLevels: ["minimal", "low", "medium", "high", "xhigh"],
  },
]

// What the chat API's `/tools` would list.
const tools: AgentTool[] = [
  { name: "set_plan", description: "Lays out the steps of a task." },
  { name: "update_plan_step", description: "Marks a step started or done." },
  { name: "add_note", description: "Saves a note beside the chat." },
  { name: "calculate", description: "Evaluates an arithmetic expression." },
  { name: "search_docs", description: "Finds passages in the team's docs." },
]

const suggestions = [
  {
    title: "Plan the billing launch",
    label: "plan, progress and a note",
    prompt: "Plan the billing launch",
  },
  {
    title: "What's our refund policy?",
    label: "cited sources",
    prompt: "What's our refund policy?",
  },
]

// The demo already uses ⌘K for its own search.
const History = () => (
  <ChatHistoryPalette
    adkUrl={undefined}
    appName="assistant"
    userId="demo"
    shortcut={false}
  />
)

function DemoToolsMenu() {
  const [off, setOff] = React.useState<string[]>([])
  return (
    <ToolsMenu
      tools={tools}
      enabledNames={tools
        .map((each) => each.name)
        .filter((name) => !off.includes(name))}
      setToolEnabled={(name, on) =>
        setOff((current) =>
          on ? current.filter((each) => each !== name) : [...current, name]
        )
      }
    />
  )
}

function WorkspaceDemo() {
  const { runtime, modelSettings } = useAdkAssistant({
    stream: workspaceMockStream,
    models,
    thinkingLevels: extendedThinkingLevels,
  })
  return (
    <AssistantScreen
      sidebar={false}
      className="h-[720px] min-w-0"
      runtime={runtime}
      showModels
      modelSettings={modelSettings}
      title="Assistant"
      welcome={{
        title: "How can I help?",
        description:
          "A scripted agent: try a suggestion to watch it plan, keep notes and cite its sources.",
      }}
      suggestions={suggestions}
      commands={workspaceCommands}
      composerLead={ProgressDock}
      composerActions={<DemoToolsMenu />}
      composerTrailingActions={History}
      toolkit={toolUIs}
      approvals={approvalViews}
      sources={workspaceSources}
      actions={
        <>
          <PlanButton />
          <NotesButton />
        </>
      }
      aside={
        <>
          <NotesPanel />
          <PlanPanel />
          <CanvasPanel />
        </>
      }
    />
  )
}

export function AgentWorkspaceSection() {
  return (
    <SectionPage
      icon="robot"
      eyebrow="Workspace"
      title="Agent workspace"
      description="Everything around the assistant's conversation that makes a capable agent's work legible: progress beside the composer, the plan, notes and canvas panels, cited sources, response and context statistics, a chat history palette, follow-up questions, a tools menu, and cards for the agent's tools. It plugs into AssistantScreen's slots."
    >
      <Specimen
        title="Live"
        variant="flush"
        description="A scripted ADK agent, no backend. Plan the billing launch to see the progress strip above the composer, the plan (3/5 in the top bar), a saved note and an answer with a table, maths, a Mermaid diagram and highlighted code. Ask about the refund policy for numbered citations and their sources under the answer."
        code={`const connection = { adkUrl: "/api/v1/agents", appName: "assistant", userId }
const History = () => <ChatHistoryPalette {...connection} />
const Followups = () => <FollowUps {...connection} />

function App() {
  const agent = useAgentWorkspace({ ...connection, chatApi: true })
  return (
    <AssistantScreen
      sidebar={false}
      runtime={agent.runtime}
      artifacts={agent.artifacts}
      {...agent.modelSection}
      commands={workspaceCommands}
      composerLead={ProgressDock}
      composerActions={<>
        <ToolsMenu {...agent.tools} />
        <ContextMeter {...connection} catalog={agent.catalog} defaultModel={agent.defaultModel} />
      </>}
      composerTrailingActions={History}
      messageMeta={MessageStats}
      followUps={Followups}
      toolkit={toolUIs}
      approvals={approvalViews}
      onOpenArtifact={(name) => useCanvas.getState().openDocument(name)}
      actions={<><PlanButton /><NotesButton /></>}
      aside={<><NotesPanel /><PlanPanel /><CanvasPanel /></>}
    />
  )
}`}
      >
        <WorkspaceDemo />
      </Specimen>
      <Specimen
        title="What it expects of the agent"
        description="The workspace reads conventions the chat API's agent (adk-chat) follows; a plain ADK API server gets the assistant without the parts that need them."
      >
        <dl className="grid grid-cols-[max-content_1fr] gap-x-6 gap-y-2.5 text-xs">
          {[
            [
              "plan",
              "Session state `{ id, title, steps: [{ text, status, started_at?, finished_at? }] }`, kept by the set_plan and update_plan_step tools. The progress strip shows only when the latest turn called one of them.",
            ],
            [
              "notes",
              "Session state `[{ id, text }]`, kept by add_note, remove_note and list_notes.",
            ],
            [
              "Documents",
              "ADK artifacts written by write_document; the canvas shows each version.",
            ],
            [
              "Statistics",
              "Usage from the persisted ADK events, priced from the chat API's model list. ContextMeter syncs them for MessageStats and the progress strip.",
            ],
            [
              "Chat API",
              "/apps/{app}/models, /apps/{app}/tools, …/sessions/{id}/feedback and …/sessions/{id}/follow-ups (useAgentWorkspace with chatApi).",
            ],
          ].map(([term, text]) => (
            <React.Fragment key={term}>
              <dt className="font-medium text-foreground">{term}</dt>
              <dd className="text-muted-foreground">{text}</dd>
            </React.Fragment>
          ))}
        </dl>
      </Specimen>
    </SectionPage>
  )
}
