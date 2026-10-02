import { Rocket01Icon } from "@hugeicons/core-free-icons"

import { cn } from "cn"

import {
  AssistantModal,
  AssistantScreen,
  useAdkAssistant,
  type AssistantCommand,
  type AssistantFeedback,
} from "@/components/forge/assistant"
import { toast } from "@/components/ui/toast"

import { mockAdkStream, mockArtifacts, mockSignIn } from "./assistant-mock"
import { WorkspacePreview } from "./workspace-preview"

const welcome = {
  title: "How can I help?",
  description:
    "This assistant runs the real Google ADK runtime against a simulated agent. Try a suggestion to see tools, approvals, sign-in and hand-offs.",
}

const models = [
  {
    id: "gemini-2.5-pro",
    name: "Gemini 2.5 Pro",
    description: "Most capable, for complex work",
  },
  {
    id: "gemini-2.5-flash",
    name: "Gemini 2.5 Flash",
    description: "Fast, for everyday tasks",
  },
]

const commands: AssistantCommand[] = [
  {
    id: "new",
    description: "Start a new conversation",
    icon: "plus",
    execute: (aui) => aui.threads.switchToNewThread(),
  },
  {
    id: "failing",
    description: "Ask which tasks are failing",
    icon: "failed",
    prompt: "Which tasks are failing?",
    send: true,
  },
  {
    id: "summarize",
    description: "Summarize this conversation",
    icon: "list",
    prompt: "Summarize this conversation in three bullet points.",
  },
  {
    id: "deploy",
    description: "Deploy a branch, e.g. /deploy main",
    icon: Rocket01Icon,
  },
  {
    id: "review",
    description: "Hand a pull request to the review agent",
    icon: "review",
  },
]

// A real app would store this next to the ADK session.
const recordFeedback = ({ type, text, sessionId }: AssistantFeedback) => {
  const firstLine = text.split("\n")[0] ?? ""
  toast.add({
    title: type === "positive" ? "Marked helpful" : "Marked not helpful",
    description: sessionId ? `${firstLine} (session ${sessionId})` : firstLine,
    type: "success",
  })
}

const suggestions = [
  {
    title: "Which tasks are failing?",
    label: "tool call + artifact",
    prompt: "Which tasks are failing?",
  },
  {
    title: "Delete the feat/legacy-sync branch",
    label: "asks for approval",
    prompt: "Delete the feat/legacy-sync branch",
  },
  {
    title: "Check my GitHub pull requests",
    label: "needs sign-in",
    prompt: "Connect GitHub and list my pull requests",
  },
  {
    title: "Deploy main",
    label: "asks you a question",
    prompt: "Deploy main",
  },
  {
    title: "Review pull request #482",
    label: "hands off to another agent",
    prompt: "Hand #482 over for review",
  },
]

/** The full-screen assistant, wired to the simulated ADK agent. */
export function AssistantPreview({ className }: { className?: string }) {
  const { runtime, modelSettings } = useAdkAssistant({
    stream: mockAdkStream,
    models,
    onFeedback: recordFeedback,
  })
  return (
    <AssistantScreen
      runtime={runtime}
      showModels
      modelSettings={modelSettings}
      commands={commands}
      artifacts={mockArtifacts}
      onAuthRequest={mockSignIn}
      title="Forge assistant"
      welcome={welcome}
      suggestions={suggestions}
      className={className}
    />
  )
}

/**
 * The Forge workspace with the same assistant as a floating launcher, lifted
 * above the workspace footer. `contained` pins the launcher inside this frame
 * instead of the viewport corner.
 */
export function AssistantModalPreview({
  className,
  contained = false,
  defaultOpen = false,
}: {
  className?: string
  contained?: boolean
  defaultOpen?: boolean
}) {
  const { runtime, modelSettings } = useAdkAssistant({
    stream: mockAdkStream,
    models,
    onFeedback: recordFeedback,
  })
  return (
    <div className={cn("relative", className)}>
      <WorkspacePreview className="h-full" />
      <AssistantModal
        runtime={runtime}
        showModels
        modelSettings={modelSettings}
        commands={commands}
        artifacts={mockArtifacts}
        onAuthRequest={mockSignIn}
        title="Forge assistant"
        welcome={welcome}
        suggestions={suggestions}
        defaultOpen={defaultOpen}
        className={cn("bottom-11", contained && "absolute")}
      />
    </div>
  )
}
