import { createFileRoute } from "@tanstack/react-router"

import { useShellPage } from "@/components/forge/shell"
import { ConversationList } from "@/features/conversations/components/conversation-list"
import { ConversationPage } from "@/features/conversations/components/conversation-page"
import {
  CHANNELS,
  SINCE,
  type Channel,
  type Since,
} from "@/features/conversations/lib/conversations"

type ConversationsSearch = {
  q?: string
  channel?: Channel
  since?: Exclude<Since, "all">
  conversation?: string
  conversationApp?: string
  conversationUser?: string
}

const text = (value: unknown) =>
  typeof value === "string" && value ? value : undefined

/**
 * Every conversation with the assistant, asked from inside an organization
 * or not, for site administrators (`agents:audit` on the site): found by who
 * held it, channel and when (`q`, `channel`, `since`), each opening whole
 * (`conversation`, `conversationApp`, `conversationUser`). Organizations'
 * own conversations, their agents' too, are on each one's Conversations page.
 */
export const Route = createFileRoute("/admin/conversations")({
  validateSearch: (search: Record<string, unknown>): ConversationsSearch => {
    const conversation = text(search.conversation)
    const app = text(search.conversationApp)
    const user = text(search.conversationUser)
    return {
      q: text(search.q),
      channel: Object.hasOwn(CHANNELS, String(search.channel))
        ? (search.channel as Channel)
        : undefined,
      since:
        Object.hasOwn(SINCE, String(search.since)) && search.since !== "all"
          ? (search.since as Exclude<Since, "all">)
          : undefined,
      ...(conversation &&
        app &&
        user && {
          conversation,
          conversationApp: app,
          conversationUser: user,
        }),
    }
  },
  // Site administration: the /admin layout lets only site administrators in.
  component: SiteConversationsPage,
})

function SiteConversationsPage() {
  const { q, channel, since, conversation, conversationApp, conversationUser } =
    Route.useSearch()
  const navigate = Route.useNavigate()
  useShellPage({
    header: { title: "Conversations", icon: "message", breadcrumbs: [] },
  })
  if (conversation && conversationApp && conversationUser)
    return (
      <ConversationPage
        key={`${conversationApp}/${conversationUser}/${conversation}`}
        scope={{ site: true }}
        conversation={{
          session: conversation,
          app: conversationApp,
          user: conversationUser,
        }}
        onBack={() =>
          void navigate({
            search: (prev) => ({
              ...prev,
              conversation: undefined,
              conversationApp: undefined,
              conversationUser: undefined,
            }),
          })
        }
      />
    )
  return (
    <ConversationList
      scope={{ site: true }}
      filters={{ q, channel, since }}
      onFiltersChange={(next) =>
        void navigate({
          search: {
            q: next.q || undefined,
            channel: next.channel || undefined,
            since: next.since && next.since !== "all" ? next.since : undefined,
          },
          replace: true,
        })
      }
      onOpen={(ref) =>
        void navigate({
          search: (prev) => ({
            ...prev,
            conversation: ref.session,
            conversationApp: ref.app,
            conversationUser: ref.user,
          }),
        })
      }
    />
  )
}
