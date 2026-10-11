import * as React from "react"
import { useQueryClient } from "@tanstack/react-query"

import { ErrorCallout } from "@/components/forge/feedback"
import { Icon } from "@/components/forge/icon"
import { Button } from "@/components/ui/button"
import { Field, FieldDescription, FieldLabel } from "@/components/ui/field"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectSeparator,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Spinner } from "@/components/ui/spinner"
import { toApiError, type ApiError } from "@/lib/api/index"
import { IssueMessages } from "@/features/builder/components/fields/field-issues"
import { useFieldIssues } from "@/features/builder/components/fields/field-issues-context"
import { FieldsReadOnly } from "@/features/builder/components/fields/read-only"
import { useCompanionCard } from "@/features/builder/components/settings-companion"
import {
  SettingsCompanion,
  SettingsHint,
} from "@/features/builder/components/settings-dialog"
import {
  cacheCredential,
  organizationCredentials,
  useOrganizationCredentials,
  type CredentialRecord,
} from "@/features/credentials/lib/api"
import {
  CREDENTIALS_ICON,
  originOf,
  sentToLabel,
  type CredentialLookup,
} from "@/features/credentials/lib/display"
import {
  useCredentialForm,
  type CredentialStart,
} from "@/features/credentials/lib/form"
import { CredentialConnection, CredentialFields } from "./credential-form"

// The select's choice that opens a new credential's card.
const NEW = "__new__"
const NONE = "__none__"

/**
 * How an HTTP step, an HTTP tool or an OpenAPI tool signs its requests: one
 * of the organization's credentials, picked by name, or none. "New
 * credential…" opens its form in a card beside the settings, its address
 * filled in from where the step calls; Edit opens the one picked. What's
 * wrong with the pick (deleted, not signed in, not for where it calls) shows
 * under it, from the checks.
 */
export function CredentialField({
  organizationId,
  value,
  onChange,
  credentials,
  url = "",
  issue = "credential",
  label = "Authentication",
}: {
  organizationId: string
  /** The credential's ID; empty for none. */
  value: string
  onChange: (value: string) => void
  /** The organization's credentials, by ID. */
  credentials: Record<string, CredentialLookup>
  /** Where the step or tool calls: a new credential is sent there. */
  url?: string
  /** The setting it holds, for its issues. */
  issue?: string
  label?: string
}) {
  const id = React.useId()
  const card = useCompanionCard(`credential:${id}`)
  const readOnly = React.useContext(FieldsReadOnly)
  const { issues, invalid, key } = useFieldIssues(issue)
  // Which the card shows: a new one, or the one picked.
  const [editing, setEditing] = React.useState<"new" | "picked">("new")
  const picked = value ? credentials[value] : undefined
  const entries = Object.entries(credentials).sort(([, a], [, b]) =>
    a.name.localeCompare(b.name)
  )
  const items = [
    { value: NONE, label: "None" },
    ...entries.map(([value, c]) => ({ value, label: c.name })),
    // A pick that's gone keeps its place, so the select still shows it.
    ...(value && !picked ? [{ value, label: "Deleted credential" }] : []),
    { value: NEW, label: "New credential…" },
  ]
  const openNew = () => {
    setEditing("new")
    card.show()
  }

  return (
    <Field data-invalid={invalid || undefined} data-field={key}>
      <div className="flex min-h-6 items-center justify-between gap-2">
        <FieldLabel htmlFor={id}>{label}</FieldLabel>
        {picked && !readOnly && card.canOpen && (
          <Button
            type="button"
            variant="ghost"
            size="xs"
            className="-my-1 text-muted-foreground"
            onClick={() => {
              setEditing("picked")
              card.show()
            }}
          >
            <Icon icon="settings" data-icon="inline-start" />
            Edit credential
          </Button>
        )}
      </div>
      <Select
        items={items}
        value={value || NONE}
        disabled={readOnly}
        onValueChange={(next) => {
          if (next === NEW) openNew()
          else if (typeof next === "string") onChange(next === NONE ? "" : next)
        }}
      >
        <SelectTrigger
          id={id}
          aria-invalid={invalid || undefined}
          className="w-full"
        >
          <SelectValue />
        </SelectTrigger>
        <SelectContent alignItemWithTrigger={false}>
          <SelectItem value={NONE}>None</SelectItem>
          {entries.map(([choice, c]) => (
            <SelectItem key={choice} value={choice}>
              <span className="flex min-w-0 flex-col">
                <span className="truncate">{c.name}</span>
                <span className="truncate text-2xs text-muted-foreground">
                  {c.method} · {sentToLabel(c.urls)}
                </span>
              </span>
            </SelectItem>
          ))}
          {value && !picked && (
            <SelectItem value={value}>Deleted credential</SelectItem>
          )}
          {!readOnly && card.canOpen && (
            <>
              <SelectSeparator />
              <SelectItem value={NEW}>
                <Icon icon="plus" data-icon="inline-start" />
                New credential…
              </SelectItem>
            </>
          )}
        </SelectContent>
      </Select>
      {issues.length ? (
        <IssueMessages issues={issues} />
      ) : (
        <FieldDescription>
          {picked ? (
            <>
              {picked.method}, sent only to {sentToLabel(picked.urls)}.
            </>
          ) : (
            "Its requests go without credentials. Pick one of the organization's to sign them in: an API key, a token, basic auth or OAuth."
          )}
        </FieldDescription>
      )}
      <CredentialCard
        card={`credential:${id}`}
        organizationId={organizationId}
        credentialId={editing === "picked" ? value : undefined}
        start={{
          name: originOf(url)?.replace(/^https?:\/\//, ""),
          urls: originOf(url) ? [originOf(url)!] : undefined,
        }}
        onSaved={(record) => {
          onChange(record.id)
          card.hide()
        }}
        onClose={card.hide}
      />
    </Field>
  )
}

/**
 * A credential's form in a card beside the settings: a new one, or the one
 * a step picked. Unlike the step's settings, it's saved with Save.
 */
function CredentialCard({
  card,
  organizationId,
  credentialId,
  start,
  onSaved,
  onClose,
}: {
  card: string
  organizationId: string
  /** The credential to edit; none adds one. */
  credentialId?: string
  start: CredentialStart
  onSaved: (record: CredentialRecord) => void
  onClose: () => void
}) {
  const { open } = useCompanionCard(card)
  const list = useOrganizationCredentials(organizationId, { enabled: open })
  const credential = credentialId
    ? list.data?.find((c) => c.id === credentialId)
    : undefined
  return (
    <SettingsCompanion
      id={card}
      title={credential ? credential.name : "New credential"}
      description="How requests sign in to an API, and where it may be sent"
      icon={CREDENTIALS_ICON}
      size="sheet"
      closeLabel="Close without saving"
      bodyClassName="flex flex-col gap-4 px-5 py-4"
    >
      {credentialId && !credential ? (
        list.isPending ? (
          <Spinner />
        ) : (
          <p className="text-xs text-muted-foreground">
            That credential was deleted.
          </p>
        )
      ) : (
        <CredentialCardForm
          // A new form for each credential shown.
          key={credential?.id ?? "new"}
          organizationId={organizationId}
          credential={credential}
          start={start}
          onSaved={onSaved}
          onClose={onClose}
        />
      )}
    </SettingsCompanion>
  )
}

function CredentialCardForm({
  organizationId,
  credential,
  start,
  onSaved,
  onClose,
}: {
  organizationId: string
  credential?: CredentialRecord
  start: CredentialStart
  onSaved: (record: CredentialRecord) => void
  onClose: () => void
}) {
  const form = useCredentialForm(credential, start)
  const client = useQueryClient()
  const scoped = organizationCredentials.scope({ organizationId })
  const create = scoped.useCreate({ meta: { silent: true } })
  const update = scoped.useUpdate({ meta: { silent: true } })
  const [error, setError] = React.useState<ApiError>()
  const pending = create.isPending || update.isPending

  async function save() {
    form.setSubmitted(true)
    if (form.invalid) return
    setError(undefined)
    try {
      const record = credential
        ? await update.mutateAsync({ id: credential.id, data: form.input() })
        : await create.mutateAsync(form.input())
      cacheCredential(client, record)
      form.reset()
      // Picked; one that needs signing in says so under the field, where
      // Edit credential opens it to sign in with.
      onSaved(record)
    } catch (caught) {
      setError(toApiError(caught))
    }
  }

  return (
    <div className="flex flex-col gap-4">
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
          readOnly={false}
          dirty={form.dirty}
        />
      )}
      <CredentialFields form={form} autoFocus={!credential} />
      <div className="flex items-center gap-2 border-t pt-4">
        <SettingsHint>Secrets are encrypted and never shown again</SettingsHint>
        <Button
          type="button"
          variant="outline"
          size="sm"
          className="ml-auto"
          onClick={onClose}
        >
          Cancel
        </Button>
        <Button
          type="button"
          size="sm"
          disabled={pending || (credential && !form.dirty)}
          onClick={() => void save()}
        >
          {pending && <Spinner data-icon="inline-start" />}
          {credential ? "Save" : "Add credential"}
        </Button>
      </div>
    </div>
  )
}
