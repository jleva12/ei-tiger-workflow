import * as React from "react"
import type { AssistantRuntime } from "@assistant-ui/react"

import { PageEmpty } from "@/components/forge/empty-state"
import { ErrorCallout } from "@/components/forge/feedback"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import {
  chatPageDesktopLink,
  chatPageRuntime,
  contextModelOf,
  isGone,
  isLimited,
  useChatPageInfo,
  useChatPageVisitor,
  visitorHeaders,
  type ChatPageInfo,
  type ChatPageVisitor,
} from "@/features/agents/lib/chat-page"
import { ChatPageView } from "@/features/agents/components/chat-page-view"
import {
  pageSettingsOf,
  pageTitleOf,
  resolvedTheme,
} from "@/features/agents/lib/page-settings"
import {
  useChosenTheme,
  useDocumentTheme,
  useSystemTheme,
} from "@/features/agents/lib/page-theme"
import { RUNTIME_URL } from "@/lib/runtime"
import { cn } from "@/lib/utils"

/**
 * A chat agent's chat page (`/chat/<token>`): the whole window, for people
 * outside Forge. Their conversations with it down the side, the one open
 * beside them, and the composer; nothing of Forge's own.
 *
 * On the console's runtime unless `runtime` names another (the desktop app's
 * agents are on whichever server their links are).
 */
export function ChatPage({
  token,
  runtime = RUNTIME_URL,
  offerDesktop = true,
  history = true,
  active = true,
  onRuntime,
  className = "h-dvh",
}: {
  token: string
  /** The runtime the page's link is on: `{API URL}/runtime`. */
  runtime?: string
  /**
   * Whether it may offer the desktop app (when its settings do): not where
   * it's in the app already.
   */
  offerDesktop?: boolean
  /**
   * Whether it lists the visitor's conversations (when its settings do): not
   * where they're listed around it (the desktop app's sidebar).
   */
  history?: boolean
  /**
   * Whether it's the one showing: it names the window and themes the
   * document only then, not while it's kept out of sight (the desktop app's
   * chats still replying after another agent was opened).
   */
  active?: boolean
  /** Hands over its chat's runtime, for whatever lists and opens its conversations. */
  onRuntime?: (runtime: AssistantRuntime) => void
  /** Its size: the window, unless it's placed in something. */
  className?: string
}) {
  const info = useChatPageInfo(token, runtime)
  // Who this browser is to the page: the API says, once it's found the page.
  const visitor = useChatPageVisitor(token, runtime, info.data?.app_name)
  // The browser's tab: the page's own name, or its agent's.
  const name = info.data
    ? pageTitleOf(pageSettingsOf(info.data), info.data)
    : undefined
  React.useEffect(() => {
    if (!active) return
    const previous = document.title
    document.title = name || "Chat"
    return () => {
      document.title = previous
    }
  }, [name, active])

  const url = chatPageRuntime(token, runtime)
  if (!url)
    return (
      <Notice
        className={className}
        title="This chat isn't set up"
        description="The console doesn't know where the agents run: set VITE_API_URL."
      />
    )
  if (info.data && visitor.data)
    return (
      <ChatPageScreen
        runtime={url}
        info={info.data}
        visitor={visitor.data}
        desktopLink={
          offerDesktop ? chatPageDesktopLink(token, runtime) : undefined
        }
        history={history}
        active={active}
        onRuntime={onRuntime}
        className={className}
      />
    )
  const failed = info.error ?? visitor.error
  if (failed && isGone(failed))
    return (
      <Notice
        className={className}
        title="This chat isn't available"
        description="Its link was turned off or replaced. Ask whoever shared it for a new one."
      />
    )
  if (failed && isLimited(failed))
    return (
      <Notice
        className={className}
        title="This chat isn't open to everyone"
        description="Its organization lets only some people use this agent, so its chat page doesn't answer. Ask whoever shared it."
      />
    )
  return (
    <main className={cn("flex items-start bg-background p-5", className)}>
      {failed ? (
        <ErrorCallout
          title="Couldn't open the chat"
          action={
            <Button
              variant="outline"
              size="sm"
              onClick={() =>
                void (info.error ? info.refetch() : visitor.refetch())
              }
            >
              Retry
            </Button>
          }
        >
          {failed.message}
        </ErrorCallout>
      ) : (
        <div className="flex w-full flex-col gap-3" aria-busy="true">
          <Skeleton className="h-5 w-48" />
          <Skeleton className="h-4 w-80 max-w-full" />
        </div>
      )}
    </main>
  )
}

function Notice({
  title,
  description,
  className,
}: {
  title: string
  description: string
  className: string
}) {
  return (
    <main
      className={cn(
        "flex items-center justify-center bg-background p-5",
        className
      )}
    >
      <PageEmpty
        illustration="locked"
        title={title}
        description={description}
      />
    </main>
  )
}

function ChatPageScreen({
  runtime: url,
  info,
  visitor,
  desktopLink,
  history,
  active,
  onRuntime,
  className,
}: {
  runtime: string
  info: ChatPageInfo
  /** Who this browser is to the page: whose conversations its chat reaches. */
  visitor: ChatPageVisitor
  /** The link that opens it in the desktop app, unless it's in it. */
  desktopLink: string | undefined
  history: boolean
  active: boolean
  onRuntime: ((runtime: AssistantRuntime) => void) | undefined
  className: string
}) {
  const userId = visitor.visitor_id
  const settings = React.useMemo(() => {
    const page = pageSettingsOf(info)
    return history ? page : { ...page, hide_history: true }
  }, [info, history])
  // The visitor's own pick, while the page offers its theme button.
  const [chosen, choose] = useChosenTheme(
    `forge.chat-page.theme.${info.app_name}`
  )
  const system = useSystemTheme()
  const theme =
    (settings.theme_toggle ? chosen : undefined) ??
    resolvedTheme(settings.theme, system)
  // Menus and tooltips open outside the page's region: the document wears its theme too.
  useDocumentTheme(theme, active)
  const connection = React.useMemo(
    () => ({
      url,
      appName: info.app_name,
      userId,
      headers: visitorHeaders(visitor),
    }),
    [url, info.app_name, userId, visitor]
  )
  const contextModel = React.useMemo(
    () => contextModelOf(info.model),
    [info.model]
  )
  return (
    <ChatPageView
      connection={connection}
      agent={info}
      settings={settings}
      theme={theme}
      onThemeChange={choose}
      desktopLink={settings.desktop_link ? desktopLink : undefined}
      onRuntime={onRuntime}
      contextModel={contextModel}
      className={className}
    />
  )
}
