import * as React from "react"
import type { AssistantRuntime } from "@assistant-ui/react"
import { cn } from "cn"

import { ChatPage } from "@/features/agents/components/chat-page"

import { useFollowChat } from "./conversations"
import { keyOf, type DesktopAgent } from "./library"
import { runtimeOf } from "./links"
import { MemberChat } from "./member-chat"
import type { Me, MyAgent } from "./session"

/**
 * An agent's chat in the window: the open agent's, or one still replying
 * after another agent was opened. That one stays mounted out of sight and
 * out of reach (unmounting it would close its reply's stream, and the
 * server would stop the reply) until its replies end.
 */
export function AgentChat({
  agent,
  open,
  me,
  member,
  onRuntime,
  onReplying,
}: {
  agent: DesktopAgent
  /** Whether it's the open agent's: the one showing. */
  open: boolean
  /** Who's signed in: a member agent's chat runs as them. */
  me: Me | undefined
  /** A member agent, as the list of their organizations' agents has it. */
  member: MyAgent | undefined
  /** Its chat's runtime, as it starts; none once it's gone. */
  onRuntime: (key: string, runtime: AssistantRuntime | undefined) => void
  /** Its conversations with a reply running (see `useFollowChat`). */
  onReplying: (key: string, conversations: string[]) => void
}) {
  const key = keyOf(agent)
  const [runtime, setRuntime] = React.useState<AssistantRuntime>()
  const handOver = React.useCallback(
    (runtime: AssistantRuntime) => {
      setRuntime(runtime)
      onRuntime(key, runtime)
    },
    [key, onRuntime]
  )
  React.useEffect(() => () => onRuntime(key, undefined), [key, onRuntime])
  useFollowChat(
    runtime,
    agent,
    React.useCallback(
      (conversations: string[]) => onReplying(key, conversations),
      [key, onReplying]
    )
  )

  return (
    <div
      data-slot="desktop-chat"
      // Out of sight, it still lays out: its reply keeps the thread
      // scrolled to its end, as it is when it's opened again. Transparent
      // as well as invisible: a message's entrance animation can paint a
      // frame of it over the open chat, and nothing inside undoes opacity.
      className={cn(
        "absolute inset-0",
        !open && "pointer-events-none invisible opacity-0"
      )}
      inert={!open}
    >
      {agent.kind === "member" ? (
        me && (
          <MemberChat
            member={agent}
            me={me}
            agent={member}
            active={open}
            onRuntime={handOver}
            className="h-full"
          />
        )
      ) : (
        <ChatPage
          token={agent.token}
          runtime={runtimeOf(agent.server)}
          // The sidebar lists its conversations; it's in the app already.
          history={false}
          offerDesktop={false}
          active={open}
          onRuntime={handOver}
          className="h-full"
        />
      )}
    </div>
  )
}
