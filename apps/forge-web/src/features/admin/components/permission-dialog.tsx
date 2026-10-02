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
import { Spinner } from "@/components/ui/spinner"
import { Textarea } from "@/components/ui/textarea"
import { toApiError, type ApiError } from "@/lib/api/index"
import {
  PERMISSION_KEY_PATTERN,
  type Permission,
  type PermissionInput,
} from "@/lib/access"
import { useLastNode, useOpenCount } from "./dialog-state"

type PermissionDialogProps = {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** The permission to edit; without one the dialog creates a permission. */
  permission?: Permission
  /** How many roles grant it, to say what a new key affects. */
  grantedBy?: number
  /** Saves; rejects with the API's error, which the dialog shows. */
  onSubmit: (
    input: PermissionInput,
    permission?: Permission
  ) => Promise<unknown>
}

/** Creates or edits a permission: its `resource:action` key and description. */
export function PermissionDialog({
  open,
  onOpenChange,
  permission,
  ...props
}: PermissionDialogProps) {
  const count = useOpenCount(open)
  const shown = useLastNode(permission)
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <PermissionForm
          key={count}
          permission={shown}
          onDone={() => onOpenChange(false)}
          {...props}
        />
      </DialogContent>
    </Dialog>
  )
}

function PermissionForm({
  permission,
  grantedBy = 0,
  onSubmit,
  onDone,
}: Omit<PermissionDialogProps, "open" | "onOpenChange"> & {
  onDone: () => void
}) {
  const id = React.useId()
  const [key, setKey] = React.useState(permission?.key ?? "")
  const [description, setDescription] = React.useState(
    permission?.description ?? ""
  )
  const [submitted, setSubmitted] = React.useState(false)
  const [pending, setPending] = React.useState(false)
  const [error, setError] = React.useState<ApiError>()

  const keyError =
    submitted && !PERMISSION_KEY_PATTERN.test(key.trim())
      ? "Use lowercase resource:action, like organizations:update."
      : error?.status === 409
        ? error.message
        : undefined
  const renaming = permission && key.trim() !== permission.key

  async function submit(event: React.SubmitEvent<HTMLFormElement>) {
    event.preventDefault()
    setSubmitted(true)
    if (!PERMISSION_KEY_PATTERN.test(key.trim())) return
    setPending(true)
    setError(undefined)
    try {
      await onSubmit(
        { key: key.trim(), description: description.trim() },
        permission
      )
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
          {permission ? "Edit permission" : "New permission"}
        </DialogTitle>
        <DialogDescription>
          {permission
            ? `Change ${permission.key}.`
            : "Something roles can let people do. Grant it to roles on the Roles page."}
        </DialogDescription>
      </DialogHeader>
      {error && error.status !== 409 && (
        <ErrorCallout
          title={`Couldn't ${permission ? "save" : "create"} the permission`}
        >
          {error.message}
        </ErrorCallout>
      )}
      <FieldGroup>
        <Field data-invalid={Boolean(keyError)}>
          <FieldLabel htmlFor={`${id}-key`}>Key</FieldLabel>
          <Input
            id={`${id}-key`}
            autoFocus
            className="font-mono"
            maxLength={200}
            autoComplete="off"
            spellCheck={false}
            placeholder="organizations:update"
            value={key}
            aria-invalid={Boolean(keyError)}
            onChange={(event) => {
              setKey(event.target.value)
              if (error?.status === 409) setError(undefined)
            }}
          />
          {keyError ? (
            <FieldError>{keyError}</FieldError>
          ) : (
            <FieldDescription>
              {renaming && grantedBy > 0
                ? `The ${grantedBy === 1 ? "role" : `${grantedBy} roles`} that grant it will grant the new key.`
                : "A resource and an action; either can be * for all of them."}
            </FieldDescription>
          )}
        </Field>
        <Field>
          <FieldLabel htmlFor={`${id}-description`}>Description</FieldLabel>
          <Textarea
            id={`${id}-description`}
            rows={2}
            value={description}
            onChange={(event) => setDescription(event.target.value)}
          />
          <FieldDescription>
            Optional. Shown when picking permissions.
          </FieldDescription>
        </Field>
      </FieldGroup>
      <DialogFooter>
        <DialogClose render={<Button type="button" variant="outline" />}>
          Cancel
        </DialogClose>
        <Button type="submit" disabled={pending}>
          {pending && <Spinner data-icon="inline-start" />}
          {permission ? "Save" : "Create permission"}
        </Button>
      </DialogFooter>
    </form>
  )
}
