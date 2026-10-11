import * as React from "react"

import { ErrorCallout } from "@/components/forge/feedback"
import { Button } from "@/components/ui/button"
import { Spinner } from "@/components/ui/spinner"
import { toApiError, type ApiError } from "@/lib/api/index"
import { useOpenCount } from "@/features/admin/components/dialog-state"
import {
  SettingsBody,
  SettingsDialog,
  SettingsFooter,
  SettingsGlyph,
  SettingsHeader,
  SettingsHint,
  SettingsSection,
} from "@/features/builder/components/settings-dialog"
import type {
  CredentialCreate,
  CredentialRecord,
} from "@/features/credentials/lib/api"
import {
  CREDENTIALS_ICON,
  sentToLabel,
} from "@/features/credentials/lib/display"
import { useCredentialForm } from "@/features/credentials/lib/form"
import { CredentialConnection, CredentialFields } from "./credential-form"

type CredentialDialogProps = {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** The credential to show and edit; without one the dialog adds one. */
  credential?: CredentialRecord
  /** Show it without letting it be changed (no `credentials:manage`). */
  readOnly?: boolean
  /** Adds (without `credential`); rejects with the API's error, which it shows. */
  onCreate?: (input: CredentialCreate) => Promise<unknown>
  /** Saves `credential`; rejects with the API's error, which it shows. */
  onUpdate?: (id: string, input: CredentialCreate) => Promise<unknown>
  onDisconnect?: (credential: CredentialRecord) => void
  onDelete?: (credential: CredentialRecord) => void
}

/**
 * Adds an API credential, or shows and edits one, in the same dialog as the
 * builders' steps and the MCP servers: its name, where it may be sent, and
 * how it signs in. Saved with Save: its secrets go to the API once.
 */
export function CredentialDialog({
  open,
  onOpenChange,
  credential,
  ...props
}: CredentialDialogProps) {
  const count = useOpenCount(open)
  // While it closes it keeps showing what it showed: a credential, or (null) a new one.
  const [shown, setShown] = React.useState<CredentialRecord | null | undefined>(
    open ? (credential ?? null) : undefined
  )
  const target = open ? (credential ?? null) : undefined
  if (target !== undefined && target !== shown) setShown(target)
  return (
    <SettingsDialog open={open} onOpenChange={onOpenChange}>
      {shown !== undefined && (
        <CredentialDialogForm
          // A new form for each opening, and once a new credential is added.
          key={`${count}:${shown?.id ?? "new"}`}
          credential={shown ?? undefined}
          onDone={() => onOpenChange(false)}
          {...props}
        />
      )}
    </SettingsDialog>
  )
}

function CredentialDialogForm({
  credential,
  readOnly = false,
  onCreate,
  onUpdate,
  onDisconnect,
  onDelete,
  onDone,
}: Omit<CredentialDialogProps, "open" | "onOpenChange"> & {
  onDone: () => void
}) {
  const form = useCredentialForm(credential)
  const [pending, setPending] = React.useState(false)
  const [error, setError] = React.useState<ApiError>()

  async function submit(event: React.SubmitEvent<HTMLFormElement>) {
    event.preventDefault()
    form.setSubmitted(true)
    if (form.invalid) return
    setPending(true)
    setError(undefined)
    try {
      if (credential) await onUpdate?.(credential.id, form.input())
      else await onCreate?.(form.input())
      form.reset()
    } catch (caught) {
      setError(toApiError(caught))
    } finally {
      setPending(false)
    }
  }

  return (
    <>
      <SettingsHeader
        glyph={<SettingsGlyph icon={CREDENTIALS_ICON} tone="action" />}
        title={credential ? credential.name : "New credential"}
        description={
          credential
            ? `${form.method?.label ?? credential.auth.kind} · sent to ${sentToLabel(credential.urls)}`
            : "How workflows and agents sign in to an API"
        }
        closeLabel="Close the credential's settings"
      />
      <form
        onSubmit={submit}
        noValidate
        className="flex min-h-0 flex-1 flex-col"
      >
        <SettingsBody>
          <SettingsSection className="gap-3">
            <p className="text-xs/[1.6] text-muted-foreground">
              HTTP steps, HTTP tools and OpenAPI tools pick it by name and sign
              their requests with it, only to the addresses below. Its secrets
              are encrypted and never shown again.
            </p>
            {error && (
              <ErrorCallout
                title={`Couldn't ${credential ? "save" : "add"} the credential`}
              >
                {error.message}
              </ErrorCallout>
            )}
            {credential && (
              <CredentialConnection
                credential={credential}
                readOnly={readOnly}
                dirty={form.dirty}
                onDisconnect={onDisconnect}
              />
            )}
          </SettingsSection>
          <SettingsSection>
            <CredentialFields
              form={form}
              readOnly={readOnly}
              autoFocus={!credential}
            />
          </SettingsSection>
        </SettingsBody>
        <SettingsFooter>
          {credential && !readOnly && onDelete && (
            <Button
              type="button"
              variant="destructive"
              size="sm"
              onClick={() => onDelete(credential)}
            >
              Delete credential
            </Button>
          )}
          <SettingsHint>
            {readOnly
              ? "You can't change the organization's credentials"
              : form.dirty
                ? "Unsaved changes"
                : "Secrets are encrypted and never shown again"}
          </SettingsHint>
          {readOnly ? (
            <Button
              type="button"
              size="sm"
              className="max-[600px]:ml-auto"
              onClick={onDone}
            >
              Done
            </Button>
          ) : (
            <>
              <Button
                type="button"
                variant="outline"
                size="sm"
                className="max-[600px]:ml-auto"
                onClick={onDone}
              >
                {credential && !form.dirty ? "Close" : "Cancel"}
              </Button>
              <Button
                type="submit"
                size="sm"
                disabled={pending || (credential && !form.dirty)}
              >
                {pending && <Spinner data-icon="inline-start" />}
                {credential ? "Save" : "Add credential"}
              </Button>
            </>
          )}
        </SettingsFooter>
      </form>
    </>
  )
}
