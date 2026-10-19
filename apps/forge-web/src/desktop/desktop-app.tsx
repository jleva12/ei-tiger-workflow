import * as React from "react"
import type { AssistantRuntime } from "@assistant-ui/react"
import { useQueryClient } from "@tanstack/react-query"
import { cn } from "cn"

import { PageEmpty } from "@/components/forge/empty-state"
import { ErrorCallout } from "@/components/forge/feedback"
import { PersonAvatar } from "@/components/forge/avatars"
import { Icon } from "@/components/forge/icon"
import { RailButton } from "@/components/forge/icon-rail"
import { Button } from "@/components/ui/button"
import { toast } from "@/components/ui/toast"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import {
  useDocumentTheme,
  useSystemTheme,
} from "@/features/agents/lib/page-theme"

import { AddAgentDialog } from "./add-agent-dialog"
import { AgentChat } from "./agent-chat"
import {
  deleteConversation,
  showConversation,
  useAgentConversations,
  useShownConversation,
} from "./conversations"
import { useDesktopLinks } from "./deep-links"
import { DesktopSidebar } from "./desktop-sidebar"
import { keyOf, useLibrary, type DesktopAgent } from "./library"
import { DEFAULT_SERVER } from "./links"
import { OfferedAgentDialog } from "./offered-agent-dialog"
import { nameOf, SIGNED_IN, useMe, useMyAgents, type MyAgent } from "./session"
import {
  DRAG_REGION,
  OVERLAY_TITLE_BAR,
  useTopBarsMoveWindow,
} from "./window-chrome"

const SIDEBAR_KEY = "forge.desktop.sidebar"

/**
 * The desktop app's window: a quiet frame (the title strip on macOS, the
 * app's icons down the left) around one panel, which holds the sidebar
 * (every agent, its conversations under it, the latest ones across them)
 * and the open agent's chat, drawn as its chat page draws it.
 */
export function DesktopApp() {
  const library = useLibrary()
  const client = useQueryClient()
  const [adding, setAdding] = React.useState(false)
  const [sidebar, setSidebar] = useSidebarOpen()
  const { open } = library
  const openKey = open && keyOf(open)
  // Who's signed in, and their organizations' agents (names for the chat).
  const me = useMe()
  const hasMembers = library.agents.some((agent) => agent.kind === "member")
  const myAgents = useMyAgents(hasMembers)

  // A link to the app (a page's "Open in the desktop app"): its agent
  // opens when it's here already, and is offered otherwise.
  useDesktopLinks(library.receive)
  // Without the system's title bar (macOS), the window moves by its frame
  // and the chat's top bar.
  useTopBarsMoveWindow(OVERLAY_TITLE_BAR)

  // The agents' chats: the open one's, and any still replying out of sight
  // after another was opened (closing one would stop its reply), by key.
  const [chats, setChats] = React.useState<
    ReadonlyMap<string, AssistantRuntime>
  >(new Map())
  const onRuntime = React.useCallback(
    (key: string, runtime: AssistantRuntime | undefined) =>
      setChats((chats) => {
        if (chats.get(key) === runtime) return chats
        const next = new Map(chats)
        if (runtime) next.set(key, runtime)
        else next.delete(key)
        return next
      }),
    []
  )
  // Each agent's conversations with a reply running; agents with none left out.
  const [replying, setReplying] = React.useState<
    ReadonlyMap<string, readonly string[]>
  >(new Map())
  const onReplying = React.useCallback(
    (key: string, conversations: string[]) =>
      setReplying((replying) => {
        const had = replying.get(key) ?? []
        if (had.join(" ") === conversations.join(" ")) return replying
        const next = new Map(replying)
        if (conversations.length) next.set(key, conversations)
        else next.delete(key)
        return next
      }),
    []
  )
  const chatted = library.agents.filter(
    (agent) =>
      (agent.kind !== "member" || me.data) &&
      (keyOf(agent) === openKey || replying.has(keyOf(agent)))
  )
  const runtime = openKey ? chats.get(openKey) : undefined
  const shown = useShownConversation(runtime)
  const groups = useAgentConversations(library.agents, me.data, me.error)

  // A conversation of another agent waits for its chat to start.
  const [pending, setPending] = React.useState<{ key: string; id: string }>()
  React.useEffect(() => {
    if (!pending || !runtime || pending.key !== openKey) return
    showConversation(runtime, pending.id)
      .catch((error: unknown) =>
        toast.add({
          title: "Couldn't open the conversation",
          description: error instanceof Error ? error.message : String(error),
          type: "error",
        })
      )
      .finally(() => setPending(undefined))
  }, [pending, runtime, openKey])

  const show = (id: string | undefined) => {
    if (runtime) void showConversation(runtime, id)
  }
  const openConversation = (agent: DesktopAgent, id: string) => {
    const key = keyOf(agent)
    if (key === openKey && runtime) return show(id)
    library.openAgent(key)
    setPending({ key, id })
  }
  // The open agent's row starts a new conversation; another's opens it.
  const openAgent = (agent: DesktopAgent) => {
    if (keyOf(agent) === openKey) show(undefined)
    else library.openAgent(keyOf(agent))
  }
  // One of their organizations' agents, added to run as them.
  const isAdded = (agent: MyAgent) =>
    library.agents.some(
      (a) =>
        a.kind === "member" && a.id === agent.id && a.server === DEFAULT_SERVER
    )
  const addMember = (agent: MyAgent) =>
    library.add({
      kind: "member",
      id: agent.id,
      organizationId: agent.organization_id,
      organizationName: agent.organization_name,
      server: DEFAULT_SERVER,
      name: agent.name,
      added: new Date().toISOString(),
    })
  const remove = async (agent: DesktopAgent, id: string) => {
    try {
      await deleteConversation(
        client,
        agent,
        id,
        chats.get(keyOf(agent)),
        me.data
      )
    } catch (error) {
      toast.add({
        title: "Couldn't delete the conversation",
        description: error instanceof Error ? error.message : String(error),
        type: "error",
      })
    }
  }

  const toggle = (
    <Button
      variant="ghost"
      size="icon-sm"
      aria-label={sidebar ? "Hide the sidebar" : "Show the sidebar"}
      aria-pressed={sidebar}
      title={sidebar ? "Hide the sidebar" : "Show the sidebar"}
      onClick={() => setSidebar(!sidebar)}
      className="text-rail-foreground"
    >
      <Icon icon="sidebar" />
    </Button>
  )

  return (
    <div className="flex h-dvh min-w-0 flex-col bg-muted text-foreground">
      {OVERLAY_TITLE_BAR && (
        // The title strip: the traffic lights at its start, the window
        // moves by it.
        <div
          {...DRAG_REGION}
          data-slot="title-strip"
          className="flex h-10 shrink-0 items-center gap-1 pl-[84px]"
        >
          {toggle}
        </div>
      )}
      <div className="flex min-h-0 flex-1">
        <nav
          aria-label="App"
          {...DRAG_REGION}
          className="flex w-[52px] shrink-0 flex-col items-center gap-1.5 pt-2 pb-3"
        >
          {!OVERLAY_TITLE_BAR && toggle}
          <RailButton
            icon="home"
            label="Chats"
            active
            onClick={() => setSidebar(true)}
            className="bg-background text-foreground shadow-(--shadow-raised)"
          />
          <RailButton
            icon="plus"
            label="Add agents"
            onClick={() => setAdding(true)}
          />
          {me.data && (
            <SignedIn name={nameOf(me.data)} email={me.data.user?.email} />
          )}
        </nav>
        <div
          className={cn(
            "mr-1.5 mb-1.5 flex min-w-0 flex-1 overflow-hidden rounded-xl border bg-background shadow-(--shadow-paper)",
            !OVERLAY_TITLE_BAR && "mt-1.5"
          )}
        >
          {sidebar && (
            <DesktopSidebar
              groups={groups}
              open={open}
              shown={shown}
              replying={replying}
              onNewChat={() => show(undefined)}
              onOpenAgent={openAgent}
              onOpenConversation={openConversation}
              onRemoveAgent={(agent) => library.remove(keyOf(agent))}
              onDeleteConversation={(agent, id) => void remove(agent, id)}
              onAdd={() => setAdding(true)}
            />
          )}
          <main
            data-slot="desktop-main"
            // No line under the chat's top bar: the sidebar has none either.
            className="flex min-w-0 flex-1 flex-col [&_[data-slot=topbar]]:border-b-0"
          >
            {library.failure && (
              <div className="p-3">
                <ErrorCallout title="Couldn't keep your agents">
                  {library.failure.message}
                </ErrorCallout>
              </div>
            )}
            <div className="relative min-h-0 flex-1">
              {chatted.map((agent) => (
                <AgentChat
                  key={keyOf(agent)}
                  agent={agent}
                  open={keyOf(agent) === openKey}
                  me={me.data}
                  member={
                    agent.kind === "member"
                      ? myAgents.data?.find((a) => a.id === agent.id)
                      : undefined
                  }
                  onRuntime={onRuntime}
                  onReplying={onReplying}
                />
              ))}
              {open?.kind === "member" && !me.data ? (
                <SignInNeeded failed={Boolean(me.error)} />
              ) : !open && library.loaded ? (
                <Welcome onAdd={() => setAdding(true)} />
              ) : null}
            </div>
          </main>
        </div>
      </div>
      <AddAgentDialog
        open={adding}
        onOpenChange={setAdding}
        added={isAdded}
        onAdd={addMember}
      />
      <OfferedAgentDialog
        link={library.offered}
        onAdd={library.add}
        onClose={library.dismissOffer}
      />
    </div>
  )
}

/** Whether the sidebar shows, as this person left it. */
function useSidebarOpen(): [boolean, (open: boolean) => void] {
  const [open, setOpen] = React.useState(() => {
    try {
      return localStorage.getItem(SIDEBAR_KEY) !== "hidden"
    } catch {
      return true
    }
  })
  const set = React.useCallback((next: boolean) => {
    setOpen(next)
    try {
      localStorage.setItem(SIDEBAR_KEY, next ? "shown" : "hidden")
    } catch {
      // Kept until the window closes.
    }
  }, [])
  return [open, set]
}

/** Before any agent is added: what the app is for, and adding the first. */
function Welcome({ onAdd }: { onAdd: () => void }) {
  // No page yet to pick the theme: the system's.
  useDocumentTheme(useSystemTheme())
  return (
    <div {...DRAG_REGION} className="flex h-full items-center justify-center">
      <PageEmpty
        illustration="conversation"
        title="Add your first agent"
        description="Pick from your organizations' published agents to chat with them here, as you."
      >
        <Button onClick={onAdd}>
          <Icon icon="plus" data-icon="inline-start" />
          Add agents
        </Button>
      </PageEmpty>
    </div>
  )
}

/** Who's signed in, at the foot of the rail. */
function SignedIn({ name, email }: { name: string; email?: string | null }) {
  return (
    <Tooltip>
      <TooltipTrigger
        render={
          <span
            tabIndex={0}
            aria-label={`Signed in as ${name}`}
            className="mt-auto flex size-8 items-center justify-center rounded-full outline-none"
          />
        }
      >
        <PersonAvatar name={name} size="lg" className="size-7" />
      </TooltipTrigger>
      <TooltipContent side="right">
        {email && email !== name ? `${name} · ${email}` : name}
      </TooltipContent>
    </Tooltip>
  )
}

/** A member agent's chat, while no one is signed in. */
function SignInNeeded({ failed }: { failed: boolean }) {
  return (
    <div className="flex h-full items-center justify-center p-5">
      <PageEmpty
        illustration="locked"
        title={failed || !SIGNED_IN ? "Sign in to chat" : "Signing in…"}
        description={
          failed || !SIGNED_IN
            ? "This agent runs as you, and the app doesn't know who you are: your sign-in is missing or wasn't accepted."
            : undefined
        }
      />
    </div>
  )
}
