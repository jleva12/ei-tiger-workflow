import * as React from "react"

import { ErrorCallout } from "@/components/forge/feedback"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import {
  Field,
  FieldDescription,
  FieldError,
  FieldGroup,
  FieldLabel,
} from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Spinner } from "@/components/ui/spinner"
import {
  useLastNode,
  useOpenCount,
} from "@/features/admin/components/dialog-state"
import { toApiError, type ApiError } from "@/lib/api/index"
import { useRoles, type Role } from "@/lib/access"
import { useScopeAccess } from "@/lib/hierarchy"
import {
  DEFAULT_KEY_ROLE,
  EXPIRIES,
  expiryOf,
  type ApiKeyRecord,
  type Expiry,
} from "../lib/api"

export type ApiKeyInput = {
  name: string
  role: string
  expires_at: string | null
}

/**
 * Makes an API key, or renames one and changes its role: a name, one of the
 * organization's roles (those granting what you don't hold yourself can't
 * be picked), and, when it's made, how long it lasts.
 */
export function ApiKeyDialog({
  open,
  onOpenChange,
  apiKey,
  organizationId,
  organizationName,
  onSubmit,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** The key to change; without one the dialog makes one. */
  apiKey?: ApiKeyRecord
  organizationId: string
  organizationName: string
  /** Saves; rejects with the API's error, which the dialog shows. */
  onSubmit: (input: ApiKeyInput) => Promise<unknown>
}) {
  const count = useOpenCount(open)
  const shown = useLastNode(apiKey)
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <ApiKeyForm
          key={count}
          apiKey={open ? apiKey : shown}
          organizationId={organizationId}
          organizationName={organizationName}
          onSubmit={onSubmit}
          onDone={() => onOpenChange(false)}
        />
      </DialogContent>
    </Dialog>
  )
}

function ApiKeyForm({
  apiKey,
  organizationId,
  organizationName,
  onSubmit,
  onDone,
}: {
  apiKey?: ApiKeyRecord
  organizationId: string
  organizationName: string
  onSubmit: (input: ApiKeyInput) => Promise<unknown>
  onDone: () => void
}) {
  const id = React.useId()
  const roles = useRoles()
  const can = useScopeAccess(`org:${organizationId}`)
  const [name, setName] = React.useState(apiKey?.name ?? "")
  const [role, setRole] = React.useState(apiKey?.role ?? DEFAULT_KEY_ROLE)
  const [expiry, setExpiry] = React.useState<Expiry>("90")
  const [submitted, setSubmitted] = React.useState(false)
  const [pending, setPending] = React.useState(false)
  const [error, setError] = React.useState<ApiError>()

  // A role may be given only when you hold everything it grants here.
  const grantable = (candidate: Role) =>
    candidate.permissions.every((p) => can(`${p.resource}:${p.action}`))
  const offered = (roles.data ?? []).filter((r) => r.level === "org")
  const items = offered.map((r) => ({ value: r.key, label: r.name }))
  const picked = offered.find((r) => r.key === role)

  const nameError =
    submitted && !name.trim()
      ? "Give the key a name: the app that uses it, say."
      : error?.status === 409
        ? error.message
        : undefined

  async function submit(event: React.SubmitEvent<HTMLFormElement>) {
    event.preventDefault()
    setSubmitted(true)
    if (!name.trim()) return
    setPending(true)
    setError(undefined)
    try {
      await onSubmit({
        name: name.trim(),
        role,
        expires_at: apiKey ? null : expiryOf(expiry),
      })
      onDone()
    } catch (caught) {
      setError(toApiError(caught))
    } finally {
      setPending(false)
    }
  }

  return (
    <form onSubmit={submit} noValidate className="grid gap-6">
      <DialogHeader>
        <DialogTitle>
          {apiKey ? `Change ${apiKey.name}` : "New API key"}
        </DialogTitle>
        <DialogDescription>
          {apiKey
            ? "Apps using it keep using it; a new role applies from their next call."
            : `What an outside app sends to call ${organizationName}'s agents and workflows, instead of a person's sign-in. It can do what its role allows here.`}
        </DialogDescription>
      </DialogHeader>
      {error && error.status !== 409 && (
        <ErrorCallout title={`Couldn't ${apiKey ? "save" : "make"} the key`}>
          {error.message}
        </ErrorCallout>
      )}
      <FieldGroup>
        <Field data-invalid={Boolean(nameError)}>
          <FieldLabel htmlFor={`${id}-name`}>Name</FieldLabel>
          <Input
            id={`${id}-name`}
            autoFocus
            maxLength={200}
            autoComplete="off"
            placeholder="Support website"
            value={name}
            aria-invalid={Boolean(nameError)}
            onChange={(event) => {
              setName(event.target.value)
              if (error?.status === 409) setError(undefined)
            }}
          />
          {nameError && <FieldError>{nameError}</FieldError>}
        </Field>
        <Field>
          <FieldLabel htmlFor={`${id}-role`}>Role</FieldLabel>
          <Select
            items={items}
            value={role}
            onValueChange={(value) => value && setRole(value)}
          >
            <SelectTrigger id={`${id}-role`} className="w-full">
              <SelectValue placeholder="Pick a role" />
            </SelectTrigger>
            <SelectContent alignItemWithTrigger={false}>
              {offered.map((r) => (
                <SelectItem key={r.key} value={r.key} disabled={!grantable(r)}>
                  {r.name}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <FieldDescription>
            {picked && !grantable(picked)
              ? "It grants permissions you don't hold here, so you can't give it."
              : picked?.description ||
                "API caller calls the organization's agents and workflows, and nothing else."}
          </FieldDescription>
        </Field>
        {!apiKey && (
          <Field>
            <FieldLabel htmlFor={`${id}-expiry`}>Expires</FieldLabel>
            <Select
              items={EXPIRIES.map((e) => ({ value: e.value, label: e.label }))}
              value={expiry}
              onValueChange={(value) => value && setExpiry(value as Expiry)}
            >
              <SelectTrigger id={`${id}-expiry`} className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent alignItemWithTrigger={false}>
                {EXPIRIES.map((e) => (
                  <SelectItem key={e.value} value={e.value}>
                    {e.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <FieldDescription>
              After that, apps using it are refused. Make a new one before it
              does.
            </FieldDescription>
          </Field>
        )}
      </FieldGroup>
      <DialogFooter>
        <DialogClose render={<Button type="button" variant="outline" />}>
          Cancel
        </DialogClose>
        <Button
          type="submit"
          disabled={pending || (picked ? !grantable(picked) : false)}
        >
          {pending && <Spinner data-icon="inline-start" />}
          {apiKey ? "Save" : "Make key"}
        </Button>
      </DialogFooter>
    </form>
  )
}
