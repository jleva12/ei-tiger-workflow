import { Button } from "@/components/ui/button"
import { Icon } from "@/components/forge/icon"
import { AssistantMark } from "@/components/forge/assistant"
import { AssistantModalPreview, AssistantPreview } from "../assistant-preview"
import { CodeBlock, SectionPage, Specimen } from "../specimen"

export function AssistantSection() {
  return (
    <SectionPage
      icon="sparkles"
      eyebrow="Workspace"
      title="AI assistant"
      description="A full-screen assistant on assistant-ui and the Google ADK runtime, in the workspace shell: conversations in the sidebar, the active agent and saved artifacts in the header, streaming markdown, reasoning, tool calls with inline approvals, cards for ADK sign-in and input requests, voice input through the browser's speech recognition, / commands, and an optional model section in the composer. The same agent also comes as a floating launcher for any page."
    >
      <Specimen
        title="Forge assistant"
        description="Runs the real ADK runtime against a simulated agent. Every suggestion exercises one ADK feature."
        variant="canvas"
        className="p-0 @max-[600px]/shell:p-0"
        code={`const { runtime, artifacts, modelSettings } = useAdkAssistant({
  adk: { url: "http://localhost:8000", appName: "my_agent", userId },
  models: [
    { id: "gemini-2.5-pro", name: "Gemini 2.5 Pro" },
    { id: "gemini-2.5-flash", name: "Gemini 2.5 Flash" },
  ],
})

<AssistantScreen
  runtime={runtime}
  artifacts={artifacts}
  showModels
  modelSettings={modelSettings}
  commands={[
    { id: "new", description: "Start a new conversation", icon: "plus",
      execute: (aui) => aui.threads.switchToNewThread() },
    { id: "deploy", description: "Deploy a branch, e.g. /deploy main" },
  ]}
  onAuthRequest={openOAuthPopup}
  title="Forge assistant"
  welcome={{ title: "How can I help?" }}
  suggestions={["Which tasks are failing?"]}
/>`}
      >
        <div className="flex items-center justify-between gap-3 border-b bg-background px-4 py-2.5">
          <span className="flex items-center gap-2 text-2xs text-muted-foreground">
            <Icon icon="robot" size={14} />
            Simulated ADK agent
          </span>
          <Button
            variant="ghost"
            size="xs"
            className="ml-auto text-muted-foreground"
            nativeButton={false}
            render={<a href="#assistant-app" />}
          >
            Open full screen
            <Icon icon="external" data-icon="inline-end" />
          </Button>
        </div>
        <div className="p-4">
          <div className="overflow-hidden rounded-(--radius-band) border bg-background shadow-(--shadow-float)">
            <AssistantPreview className="h-[760px] min-w-0" />
          </div>
        </div>
      </Specimen>

      <Specimen
        title="Floating assistant"
        description="AssistantModal puts the same agent behind a launcher in the corner of any page: a resizable panel with the conversation, past conversations and a new-conversation button. It opens itself when the agent starts replying."
        variant="canvas"
        className="p-0 @max-[600px]/shell:p-0"
        code={`const { runtime, artifacts, modelSettings } = useAdkAssistant({
  adk: { url: "http://localhost:8000", appName: "my_agent", userId },
})

// Once, anywhere in the app: the launcher sits in the viewport corner.
<AssistantModal
  runtime={runtime}
  artifacts={artifacts}
  title="Forge assistant"
  showModels
  modelSettings={modelSettings}
  commands={commands}
/>`}
      >
        <div className="flex items-center justify-between gap-3 border-b bg-background px-4 py-2.5">
          <span className="flex items-center gap-2 text-2xs text-muted-foreground">
            <Icon icon="robot" size={14} />
            Open the launcher in the bottom-right corner of the workspace
          </span>
          <Button
            variant="ghost"
            size="xs"
            className="ml-auto text-muted-foreground"
            nativeButton={false}
            render={<a href="#assistant-modal-app" />}
          >
            Open full screen
            <Icon icon="external" data-icon="inline-end" />
          </Button>
        </div>
        <div className="p-4">
          <div className="overflow-hidden rounded-(--radius-band) border bg-background shadow-(--shadow-float)">
            <AssistantModalPreview contained className="h-[760px] min-w-0" />
          </div>
        </div>
      </Specimen>

      <Specimen
        title="Assistant mark"
        description="The launcher's icon: a chat bubble in the agent-orb gradient with a spark. Hovering the launcher twinkles the spark and warms the gradient; while the agent works, the spark turns."
        code={`<AssistantMark className="size-8" />
<AssistantMark active /> // the agent is working`}
      >
        <div className="flex flex-wrap items-end gap-8">
          {[22, 32, 48, 72].map((size) => (
            <div key={size} className="flex flex-col items-center gap-2">
              <AssistantMark style={{ width: size, height: size }} />
              <span className="text-2xs text-muted-foreground">{size}px</span>
            </div>
          ))}
          <div className="flex flex-col items-center gap-2">
            <AssistantMark active className="size-12" />
            <span className="text-2xs text-muted-foreground">Working</span>
          </div>
          <div className="flex flex-col items-center gap-2">
            <span className="group/launcher grid size-11 place-items-center rounded-full border border-border/60 bg-background shadow-(--shadow-float) hover:border-border">
              <AssistantMark className="size-[26px]" />
            </span>
            <span className="text-2xs text-muted-foreground">
              Launcher (hover)
            </span>
          </div>
        </div>
      </Specimen>

      <Specimen
        title="Connect your agent"
        description="Direct mode talks to an ADK server (adk api_server) and turns its sessions into the conversation list. Proxy mode posts to your own route built with createAdkApiRoute."
      >
        <CodeBlock
          code={`// Direct: an ADK server. Sessions become conversations; artifacts download.
const { runtime, artifacts } = useAdkAssistant({
  adk: { url: import.meta.env.VITE_ADK_URL, appName: "my_agent", userId },
  headers: () => ({ Authorization: \`Bearer \${session.token}\` }),
  // Thumbs up / down on replies; store them with the session.
  onFeedback: ({ type, sessionId, message }) =>
    api.post("/feedback", { type, sessionId, messageId: message.id }),
})

// Proxy: your API route (see @assistant-ui/react-google-adk/server).
const { runtime } = useAdkAssistant({ api: "/api/chat" })

// Edit and regenerate need a checkpoint to fork from on the server.
useAdkAssistant({ api: "/api/chat", getCheckpointId: resolveCheckpoint })`}
        />
      </Specimen>

      <Specimen
        title="Model section"
        description="With showModels, the composer shows the agent's name, the model (when there's more than one) and the thinking level. The selection goes to the agent as ADK session state with every run, as model and thinking_level; a before_model_callback applies it."
      >
        <CodeBlock
          code={`from google.adk.agents import Agent
from google.genai import types

BUDGETS = {"off": 0, "low": 1024, "medium": 8192, "high": 24576}

def apply_model_settings(callback_context, llm_request):
    state = callback_context.state
    if model := state.get("model"):
        llm_request.model = model
    if (level := state.get("thinking_level")) in BUDGETS:
        llm_request.config.thinking_config = types.ThinkingConfig(
            thinking_budget=BUDGETS[level],
            include_thoughts=level != "off",
        )

root_agent = Agent(
    name="forge_assistant",
    model="gemini-2.5-flash",
    instruction="...",
    before_model_callback=apply_model_settings,
)`}
        />
      </Specimen>
    </SectionPage>
  )
}
