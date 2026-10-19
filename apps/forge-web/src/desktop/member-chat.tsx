import * as React from "react"
import type { AssistantRuntime } from "@assistant-ui/react"

import { ChatPageView } from "@/features/agents/components/chat-page-view"
import { contextModelOf } from "@/features/agents/lib/chat-page"
import {
  pageSettingsOf,
  resolvedTheme,
} from "@/features/agents/lib/page-settings"
import {
  useChosenTheme,
  useDocumentTheme,
  useSystemTheme,
} from "@/features/agents/lib/page-theme"

import type { MemberAgent } from "./library"
import { runtimeOf } from "./links"
import { signInHeaders, type Me, type MyAgent } from "./session"

// A member agent has no page of its own: the chat page's look, as a page
// that set nothing has it, without its history (the sidebar lists it).
const SETTINGS = pageSettingsOf({ hide_history: true })

/**
 * A member agent's chat: the chat page's, run as the person signed in,
 * through the runtime (their conversations, by their subject), with their
 * token. `agent` is what the list of their agents says of it now.
 */
export function MemberChat({
  member,
  me,
  agent,
  active = true,
  onRuntime,
  className,
}: {
  member: MemberAgent
  me: Me
  /** It, as the list of their organizations' agents has it; none until that's read. */
  agent: MyAgent | undefined
  /** Whether it's the one showing: only then does the document wear its theme. */
  active?: boolean
  onRuntime: (runtime: AssistantRuntime) => void
  className?: string
}) {
  // Each person's theme pick, per agent, as a page keeps a visitor's.
  const [chosen, choose] = useChosenTheme(`forge.desktop.theme.${member.id}`)
  const system = useSystemTheme()
  const theme = chosen ?? resolvedTheme(SETTINGS.theme, system)
  useDocumentTheme(theme, active)

  const connection = React.useMemo(
    () => ({
      url: runtimeOf(member.server),
      appName: member.id,
      userId: me.subject,
      headers: signInHeaders,
    }),
    [member.server, member.id, me.subject]
  )
  const shown = React.useMemo(
    () => ({
      name: agent?.name ?? member.name,
      description: agent?.description ?? "",
      agents: agent?.agents,
      workflows: agent?.workflows,
    }),
    [agent, member.name]
  )
  const contextModel = React.useMemo(
    () => contextModelOf(agent?.model),
    [agent?.model]
  )
  return (
    <ChatPageView
      connection={connection}
      agent={shown}
      contextModel={contextModel}
      settings={SETTINGS}
      theme={theme}
      onThemeChange={choose}
      onRuntime={onRuntime}
      className={className}
    />
  )
}
