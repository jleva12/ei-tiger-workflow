import * as React from "react"

import {
  AssistantScreen,
  knowledgeBaseSources,
  useAdkAssistant,
} from "@/components/forge/assistant/index"
import { ChatPageLook } from "@/features/agents/components/chat-page-look"
import { ChatPageThemeToggle } from "@/features/agents/components/chat-page-theme-toggle"
import {
  pageTitleOf,
  type ChatPageSettings,
} from "@/features/agents/lib/page-settings"
import { WorkflowRunsContext } from "@/features/agents/lib/workflow-run-context"
import { workflowRunToolkit } from "@/features/agents/lib/workflow-run-toolkit"
import { workflowRunFollower } from "@/features/agents/lib/workflow-runs"

// Its knowledge base tools' passages, cited in its answers: without links
// into Forge, which a page's visitors can't open.
const sources = knowledgeBaseSources()

const NO_NAMES: Readonly<Record<string, string>> = {}

/** Where a chat page's chat talks to its agent, and as whom. */
export type ChatPageConnection = {
  /** The run API: the page's link, or the console's runtime. */
  url: string
  appName: string
  userId: string
  headers?: Record<string, string> | (() => Record<string, string>)
}

/** What the page shows of its agent. */
export type ChatPageAgent = {
  name: string
  description: string
  /** Its agents by the names ADK calls them, as the canvas names them. */
  agents?: Readonly<Record<string, string>>
  /** Its workflow tools by the names the model calls them. */
  workflows?: Readonly<Record<string, string>>
}

/**
 * A chat page as its visitors see it, in its settings: its words, what it
 * leaves out, its look. The page itself (`/chat/<token>`) and its builder's
 * preview draw it alike; the preview fits it to a frame (`className`).
 */
export function ChatPageView({
  connection,
  agent,
  settings,
  theme,
  onThemeChange,
  className = "h-dvh",
}: {
  connection: ChatPageConnection
  agent: ChatPageAgent
  settings: ChatPageSettings
  /** The theme it shows: the visitor's choice, the page's own, or the visitor's system's. */
  theme: "light" | "dark"
  /** Switches it, from the page's theme button (when the page has one). */
  onThemeChange?: (theme: "light" | "dark") => void
  className?: string
}) {
  const { url, appName, userId, headers } = connection
  const { runtime, artifacts } = useAdkAssistant({
    adk: { url, appName, userId },
    headers,
    // As the builder's chat does, unless the page turned them off: a few
    // questions to ask next, after each reply.
    suggestFollowUps: settings.follow_ups,
  })
  // Its workflow tools' calls, each drawn as the run it starts, followed
  // through the same runtime.
  const workflows = agent.workflows ?? NO_NAMES
  const follower = React.useMemo(
    () =>
      workflowRunFollower({ url, appName, userId, headers, names: workflows }),
    [url, appName, userId, headers, workflows]
  )
  const toolkit = React.useMemo(
    () => workflowRunToolkit(workflows),
    [workflows]
  )
  const title = pageTitleOf(settings, agent)
  const suggestions = settings.suggestions
  const offered = React.useMemo(
    () => (suggestions.length ? suggestions : undefined),
    [suggestions]
  )
  const welcome = React.useMemo(
    () => ({
      title: settings.welcome_title.trim() || `Talk to ${title}`,
      description:
        settings.welcome_message.trim() ||
        agent.description ||
        "Ask it anything.",
    }),
    [settings.welcome_title, settings.welcome_message, title, agent.description]
  )
  // The theme button: in the top bar, or over the page's corner without one.
  const toggle =
    settings.theme_toggle && onThemeChange
      ? (floating: boolean) => (
          <ChatPageThemeToggle
            theme={theme}
            onChange={onThemeChange}
            floating={floating}
          />
        )
      : undefined
  return (
    <WorkflowRunsContext.Provider value={follower}>
      <ChatPageLook settings={settings} theme={theme} className={className}>
        {settings.hide_top_bar && toggle?.(true)}
        <AssistantScreen
          className="h-full min-w-0"
          runtime={runtime}
          artifacts={artifacts}
          toolkit={toolkit}
          sources={sources}
          title={title}
          welcome={welcome}
          // Its prompts, under the message box while a conversation is empty.
          suggestions={offered}
          placeholder={settings.placeholder.trim() || undefined}
          composerNote={settings.footer_note.trim() || undefined}
          // What the page leaves out: its visitor's conversations, its top bar.
          sidebar={settings.hide_history ? false : undefined}
          topbar={!settings.hide_top_bar}
          actions={settings.hide_top_bar ? undefined : toggle?.(false)}
          // Its agents' names: over their replies, and on hand-offs to them.
          agents={agent.agents}
        />
      </ChatPageLook>
    </WorkflowRunsContext.Provider>
  )
}
