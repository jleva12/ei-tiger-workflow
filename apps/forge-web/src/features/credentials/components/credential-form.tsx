import * as React from "react"
import { Delete02Icon } from "@hugeicons/core-free-icons"

import { ErrorCallout } from "@/components/forge/feedback"
import { Icon } from "@/components/forge/icon"
import { Chip } from "@/components/forge/status"
import { Button } from "@/components/ui/button"
import {
  Field,
  FieldDescription,
  FieldError,
  FieldLabel,
} from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { Spinner } from "@/components/ui/spinner"
import { Textarea } from "@/components/ui/textarea"
import { toApiError } from "@/lib/api/index"
import { formatRelative } from "@/lib/format"
import { AuthFields } from "@/features/mcp-servers/components/mcp-server-dialog"
import type { CredentialRecord } from "@/features/credentials/lib/api"
import {
  CREDENTIAL_URL,
  draftId,
  signInWith,
  type CredentialForm,
} from "@/features/credentials/lib/form"

/*
 * A credential's form, as the Credentials page's dialog and a step's or
 * tool's "New credential" card show it: its name, where it may be sent, and
 * how it signs in (any auth method the admin API offers, its form drawn from
 * the method's fields). Secrets go to the API once, on Save, and are never
 * shown again.
 */

/** A credential's fields: its name, where it may go, and how it signs in. */
export function CredentialFields({
  form,
  readOnly = false,
  autoFocus = false,
}: {
  form: CredentialForm
  readOnly?: boolean
  autoFocus?: boolean
}) {
  const id = React.useId()
  const shown = (message: string | undefined) =>
    form.submitted ? message : undefined
  return (
    <>
      <Field data-invalid={Boolean(shown(form.problems.name))}>
        <FieldLabel htmlFor={`${id}-name`}>Name</FieldLabel>
        <Input
          id={`${id}-name`}
          autoFocus={autoFocus && !readOnly}
          maxLength={200}
          autoComplete="off"
          disabled={readOnly}
          value={form.name}
          placeholder="Orders API"
          aria-invalid={Boolean(shown(form.problems.name))}
          onChange={(event) => form.setName(event.target.value)}
        />
        {shown(form.problems.name) && (
          <FieldError>{form.problems.name}</FieldError>
        )}
      </Field>
      <UrlRows
        form={form}
        readOnly={readOnly}
        error={shown(form.problems.urls)}
      />
      <Field>
        <FieldLabel htmlFor={`${id}-description`}>Description</FieldLabel>
        <Textarea
          id={`${id}-description`}
          rows={2}
          disabled={readOnly}
          value={form.description}
          placeholder="What it reaches, and as whom"
          onChange={(event) => form.setDescription(event.target.value)}
        />
      </Field>
      <div className="flex flex-col gap-3 border-t pt-4">
        <span className="text-xs font-medium text-foreground">
          How it signs in
        </span>
        <AuthFields
          id={`${id}-auth`}
          methods={form.methods}
          kind={form.kind}
          method={form.method}
          onKind={form.pickKind}
          settings={form.values}
          onSetting={form.setSetting}
          secrets={form.secrets}
          onSecret={form.setSecret}
          saved={form.saved}
          cleared={form.cleared}
          onClear={form.clearSecret}
          problems={form.submitted ? form.problems.fields : {}}
          readOnly={readOnly}
        />
      </div>
    </>
  )
}

/** Where a credential may be sent: a URL a row, added and removed in place. */
function UrlRows({
  form,
  readOnly,
  error,
}: {
  form: CredentialForm
  readOnly: boolean
  error?: string
}) {
  const [added, setAdded] = React.useState<string>()
  const { urls } = form
  return (
    <div role="group" aria-label="Sent only to" className="flex flex-col gap-2">
      <span className="text-xs font-medium text-foreground">Sent only to</span>
      {urls.map((row, index) => (
        <div key={row.id} className="flex items-center gap-1.5">
          <Input
            aria-label={`Address ${index + 1}`}
            className="font-mono text-xs"
            value={row.url}
            placeholder="https://api.example.com"
            spellCheck={false}
            autoComplete="off"
            disabled={readOnly}
            autoFocus={row.id === added}
            aria-invalid={
              (Boolean(error) &&
                row.url.trim() !== "" &&
                !CREDENTIAL_URL.test(row.url.trim())) ||
              undefined
            }
            onChange={(event) =>
              form.setUrls(
                urls.map((u) =>
                  u.id === row.id ? { ...u, url: event.target.value } : u
                )
              )
            }
          />
          {!readOnly && urls.length > 1 && (
            <Button
              type="button"
              variant="ghost"
              size="icon"
              aria-label={`Remove ${row.url.trim() || "address"}`}
              className="text-muted-foreground hover:text-destructive"
              onClick={() => form.setUrls(urls.filter((u) => u.id !== row.id))}
            >
              <Icon icon={Delete02Icon} />
            </Button>
          )}
        </div>
      ))}
      {error ? (
        <FieldError>{error}</FieldError>
      ) : (
        <FieldDescription>
          Requests to these addresses, or under their paths, carry it; a request
          anywhere else is refused, so it can&apos;t be sent to another host.
        </FieldDescription>
      )}
      {!readOnly && (
        <Button
          type="button"
          variant="outline"
          size="sm"
          className="border-dashed text-muted-foreground"
          onClick={() => {
            const row = { id: draftId(), url: "" }
            setAdded(row.id)
            form.setUrls([...urls, row])
          }}
        >
          <Icon icon="plus" data-icon="inline-start" />
          Add an address
        </Button>
      )}
    </div>
  )
}

/** A sign-in credential's state: who signed in, and signing in or out. */
export function CredentialConnection({
  credential,
  readOnly,
  dirty,
  onDisconnect,
}: {
  credential: CredentialRecord
  readOnly: boolean
  /** The form has unsaved changes: sign in with what's saved first. */
  dirty: boolean
  onDisconnect?: (credential: CredentialRecord) => void
}) {
  const [connecting, setConnecting] = React.useState(false)
  const [failed, setFailed] = React.useState<string>()
  const { auth } = credential
  if (!auth.interactive) return null
  const connect = async () => {
    setConnecting(true)
    setFailed(undefined)
    try {
      // It navigates away when it works.
      await signInWith(credential)
    } catch (caught) {
      setFailed(toApiError(caught).message)
      setConnecting(false)
    }
  }
  return (
    <div
      role="group"
      aria-label="Sign-in"
      className="flex flex-col gap-2 rounded-(--radius-item) border bg-muted/40 p-3"
    >
      <div className="flex flex-wrap items-center gap-2">
        <Chip tone={auth.connected ? "success" : "warning"}>
          {auth.connected ? "Signed in" : "Needs sign-in"}
        </Chip>
        {auth.connected && (
          <span className="text-2xs text-muted-foreground">
            {auth.connected_by_name && `By ${auth.connected_by_name}`}
            {auth.connected_at && ` ${formatRelative(auth.connected_at)}`}
            {auth.scope && (
              <>
                {" · "}
                <span className="font-mono">{auth.scope}</span>
              </>
            )}
          </span>
        )}
        {!readOnly && (
          <div className="ml-auto flex gap-1.5">
            {auth.connected && onDisconnect && (
              <Button
                type="button"
                size="sm"
                variant="ghost"
                onClick={() => onDisconnect(credential)}
              >
                Sign out
              </Button>
            )}
            <Button
              type="button"
              size="sm"
              variant={auth.connected ? "outline" : "default"}
              disabled={connecting || dirty}
              title={dirty ? "Save your changes first" : undefined}
              onClick={() => void connect()}
            >
              {connecting && <Spinner data-icon="inline-start" />}
              {auth.connected ? "Sign in again" : "Sign in"}
            </Button>
          </div>
        )}
      </div>
      <p className="text-2xs/[1.6] text-muted-foreground">
        {auth.connected
          ? "Every step and tool using this credential signs in as this sign-in."
          : "Someone signs in once; Forge keeps the tokens and refreshes them for every run."}
      </p>
      {failed && (
        <ErrorCallout title="Couldn't start signing in">{failed}</ErrorCallout>
      )}
    </div>
  )
}
