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
  cacheCredential,
  finishCredentialOAuth,
  type CredentialRecord,
} from "@/features/credentials/lib/api"
import {
  CREDENTIAL_OAUTH_RETURN_KEY,
  CREDENTIALS_ICON,
  type CredentialOAuthReturn,
} from "@/features/credentials/lib/display"

type CallbackSearch = {
  state?: string
  code?: string
  error?: string
  error_description?: string
}

const text = (value: unknown) =>
  typeof value === "string" && value ? value : undefined

/**
 * Where a credential's authorization server sends the browser back after
 * signing in (an OAuth sign-in credential): hands what came back to the admin
 * API, which finishes the sign-in and keeps the tokens, then goes back to the
 * page the sign-in started on (a builder, or Credentials).
 */
export const Route = createFileRoute("/oauth/credentials/callback")({
  validateSearch: (search: Record<string, unknown>): CallbackSearch => ({
    state: text(search.state),
    code: text(search.code),
    error: text(search.error),
    error_description: text(search.error_description),
  }),
  component: CredentialOAuthCallback,
})

function readReturn(): CredentialOAuthReturn | undefined {
  try {
    const kept = sessionStorage.getItem(CREDENTIAL_OAUTH_RETURN_KEY)
    const back = kept ? (JSON.parse(kept) as CredentialOAuthReturn) : undefined
    // Only a page of this app's: a path, not another site's address.
    return back && back.path.startsWith("/") && !back.path.startsWith("//")
      ? back
      : undefined
  } catch {
    return undefined
  }
}

function CredentialOAuthCallback() {
  const search = Route.useSearch()
  const navigate = Route.useNavigate()
  const client = useQueryClient()
  const [refused, setRefused] = React.useState<string>()
  const back = React.useMemo(() => readReturn(), [])
  const failure = search.state
    ? refused
    : "The sign-in came back without its state. Start it again from the credential."
  // Once, though development renders effects twice: a state works once.
  const sent = React.useRef(false)

  useShellPage({
    header: { title: "Signing in with a credential", icon: CREDENTIALS_ICON },
  })

  const toCredentials = React.useCallback(
    (organizationId: string, credentialId: string) =>
      void navigate({
        to: "/organizations/$organizationId",
        params: { organizationId },
        search: { view: "credentials", credential: credentialId },
        replace: true,
      }),
    [navigate]
  )

  React.useEffect(() => {
    if (sent.current || !search.state) return
    sent.current = true
    finishCredentialOAuth({
      state: search.state,
      code: search.code,
      error: search.error,
      error_description: search.error_description,
    }).then(
      (credential: CredentialRecord) => {
        try {
          sessionStorage.removeItem(CREDENTIAL_OAUTH_RETURN_KEY)
        } catch {
          // Nothing kept.
        }
        cacheCredential(client, credential)
        toast.add({
          title: `Signed in with ${credential.name}`,
          description: "Steps and tools that use it send its token.",
          type: "success",
        })
        // Back where the sign-in started, when it was this credential's.
        if (back?.credentialId === credential.id)
          void navigate({ href: back.path, replace: true })
        else toCredentials(credential.organization_id, credential.id)
      },
      (caught: unknown) => setRefused(toApiError(caught).message)
    )
  }, [back, client, navigate, search, toCredentials])

  if (!failure) {
    return (
      <PageEmpty
        title="Finishing the sign-in…"
        description="Forge is exchanging what came back for the credential's tokens."
      >
        <Spinner />
      </PageEmpty>
    )
  }
  return (
    <ErrorCallout
      title="Couldn't sign in with the credential"
      action={
        back ? (
          <Button
            variant="outline"
            size="sm"
            onClick={() =>
              toCredentials(back.organizationId, back.credentialId)
            }
          >
            Back to the credential
          </Button>
        ) : (
          <Button
            variant="outline"
            size="sm"
            nativeButton={false}
            render={<Link to="/" />}
          >
            Home
          </Button>
        )
      }
    >
      {failure}
    </ErrorCallout>
  )
}
