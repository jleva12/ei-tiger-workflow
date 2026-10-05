import * as React from "react"
import { useQueryClient } from "@tanstack/react-query"
import { createFileRoute, Link } from "@tanstack/react-router"

import { PageEmpty } from "@/components/forge/empty-state"
import { ErrorCallout } from "@/components/forge/feedback"
import { useShellPage } from "@/components/forge/shell"
import { Button } from "@/components/ui/button"
import { Spinner } from "@/components/ui/spinner"
import { toast } from "@/components/ui/toast"
import { toApiError } from "@/lib/api/index"
import {
  cacheMcpServer,
  finishMcpOAuth,
  type McpServerRecord,
} from "@/features/mcp-servers/lib/api"
import {
  MCP_SERVERS_ICON,
  OAUTH_RETURN_KEY,
  plural,
  type OAuthReturn,
} from "@/features/mcp-servers/lib/display"

type CallbackSearch = {
  state?: string
  code?: string
  error?: string
  error_description?: string
}

const text = (value: unknown) =>
  typeof value === "string" && value ? value : undefined

/**
 * Where an MCP server's authorization server sends the browser back after
 * signing in: hands what came back to the admin API, which finishes the
 * sign-in and checks the server, then goes back to the server's dialog.
 */
export const Route = createFileRoute("/oauth/mcp/callback")({
  validateSearch: (search: Record<string, unknown>): CallbackSearch => ({
    state: text(search.state),
    code: text(search.code),
    error: text(search.error),
    error_description: text(search.error_description),
  }),
  component: McpOAuthCallback,
})

function readReturn(): OAuthReturn | undefined {
  try {
    const kept = sessionStorage.getItem(OAUTH_RETURN_KEY)
    return kept ? (JSON.parse(kept) as OAuthReturn) : undefined
  } catch {
    return undefined
  }
}

function McpOAuthCallback() {
  const search = Route.useSearch()
  const navigate = Route.useNavigate()
  const client = useQueryClient()
  const [refused, setRefused] = React.useState<string>()
  const back = React.useMemo(() => readReturn(), [])
  const failure = search.state
    ? refused
    : "The sign-in came back without its state. Start it again from the server."
  // Once, though development renders effects twice: a state works once.
  const sent = React.useRef(false)

  useShellPage({
    header: { title: "Signing in to an MCP server", icon: MCP_SERVERS_ICON },
  })

  const goTo = React.useCallback(
    (organizationId: string, serverId: string) =>
      void navigate({
        to: "/organizations/$organizationId",
        params: { organizationId },
        search: { view: "mcp-servers", mcpServer: serverId },
        replace: true,
      }),
    [navigate]
  )

  React.useEffect(() => {
    if (sent.current || !search.state) return
    sent.current = true
    finishMcpOAuth({
      state: search.state,
      code: search.code,
      error: search.error,
      error_description: search.error_description,
    }).then(
      (server: McpServerRecord) => {
        try {
          sessionStorage.removeItem(OAUTH_RETURN_KEY)
        } catch {
          // Nothing kept.
        }
        cacheMcpServer(client, server)
        toast.add({
          title: `Signed in to ${server.name}`,
          description:
            server.status === "ok"
              ? `It has ${plural(server.tools.length, "tool")}.`
              : (server.last_error ?? undefined),
          type: server.status === "ok" ? "success" : "warning",
        })
        goTo(server.organization_id, server.id)
      },
      (caught: unknown) => setRefused(toApiError(caught).message)
    )
  }, [client, goTo, search])

  if (!failure) {
    return (
      <PageEmpty
        title="Finishing the sign-in…"
        description="Forge is exchanging what came back for the server's tokens, then checking the server."
      >
        <Spinner />
      </PageEmpty>
    )
  }
  return (
    <ErrorCallout
      title="Couldn't sign in to the MCP server"
      action={
        back ? (
          <Button
            variant="outline"
            size="sm"
            onClick={() => goTo(back.organizationId, back.serverId)}
          >
            Back to the server
          </Button>
        ) : (
          <Button variant="outline" size="sm" nativeButton={false} render={<Link to="/" />}>
            Home
          </Button>
        )
      }
    >
      {failure}
    </ErrorCallout>
  )
}
