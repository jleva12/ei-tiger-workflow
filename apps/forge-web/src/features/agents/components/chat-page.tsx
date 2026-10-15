import * as React from "react"

import { PageEmpty } from "@/components/forge/empty-state"
import { ErrorCallout } from "@/components/forge/feedback"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import {
  chatPageRuntime,
  isGone,
  useChatPageInfo,
  visitorId,
  type ChatPageInfo,
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

/**
 * A chat agent's chat page (`/chat/<token>`): the whole window, for people
 * outside Forge. Their conversations with it down the side, the one open
 * beside them, and the composer; nothing of Forge's own.
 */
export function ChatPage({ token }: { token: string }) {
  const info = useChatPageInfo(token)
  // The browser's tab: the page's own name, or its agent's.
  const name = info.data
    ? pageTitleOf(pageSettingsOf(info.data), info.data)
    : undefined
  React.useEffect(() => {
    const previous = document.title
    document.title = name || "Chat"
    return () => {
      document.title = previous
    }
  }, [name])

  const runtime = chatPageRuntime(token)
  if (!runtime)
    return (
      <Notice
        title="This chat isn't set up"
        description="The console doesn't know where the agents run: set VITE_API_URL."
      />
    )
  if (info.data) return <ChatPageScreen runtime={runtime} info={info.data} />
  if (info.error && isGone(info.error))
    return (
      <Notice
        title="This chat isn't available"
        description="Its link was turned off or replaced. Ask whoever shared it for a new one."
      />
    )
  return (
    <main className="flex h-dvh items-start bg-background p-5">
      {info.error ? (
        <ErrorCallout
          title="Couldn't open the chat"
          action={
            <Button
              variant="outline"
              size="sm"
              onClick={() => void info.refetch()}
            >
              Retry
            </Button>
          }
        >
          {info.error.message}
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
}: {
  title: string
  description: string
}) {
  return (
    <main className="flex h-dvh items-center justify-center bg-background p-5">
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
}: {
  runtime: string
  info: ChatPageInfo
}) {
  const userId = React.useMemo(() => visitorId(info.app_name), [info.app_name])
  const settings = React.useMemo(() => pageSettingsOf(info), [info])
  // The visitor's own pick, while the page offers its theme button.
  const [chosen, choose] = useChosenTheme(
    `forge.chat-page.theme.${info.app_name}`
  )
  const system = useSystemTheme()
  const theme =
    (settings.theme_toggle ? chosen : undefined) ??
    resolvedTheme(settings.theme, system)
  // Menus and tooltips open outside the page's region: the document wears its theme too.
  useDocumentTheme(theme)
  const connection = React.useMemo(
    () => ({ url, appName: info.app_name, userId }),
    [url, info.app_name, userId]
  )
  return (
    <ChatPageView
      connection={connection}
      agent={info}
      settings={settings}
      theme={theme}
      onThemeChange={choose}
    />
  )
}
