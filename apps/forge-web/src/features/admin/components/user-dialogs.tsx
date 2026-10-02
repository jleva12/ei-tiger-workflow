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
import { toApiError, type ApiError } from "@/lib/api/index"
import { userName, type User, type UserInput } from "@/lib/users"
import { useLastNode, useOpenCount } from "./dialog-state"

// Matches the API's check: one @ and no spaces.
const EMAIL = /^[^@\s]+@[^@\s]+$/

type UserDialogProps = {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** The user to edit; without one the dialog adds a user. */
  user?: User
  /** Saves; rejects with the API's error, which the dialog shows. */
  onSubmit: (input: UserInput, user?: User) => Promise<unknown>
}

/** Adds a person to the user directory, or edits one. */
export function UserDialog({
  open,
  onOpenChange,
  user,
  onSubmit,
}: UserDialogProps) {
  const count = useOpenCount(open)
  const shown = useLastNode(user)
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <UserForm
          key={count}
          user={shown}
          onSubmit={onSubmit}
          onDone={() => onOpenChange(false)}
        />
      </DialogContent>
    </Dialog>
  )
}

// Matches the API's check: a letter or digit, then letters, digits, ., _ or -.
const MSID = /^[A-Za-z0-9][A-Za-z0-9._-]*$/

type FieldName = keyof UserInput

function UserForm({
  user,
  onSubmit,
  onDone,
}: Pick<UserDialogProps, "user" | "onSubmit"> & { onDone: () => void }) {
  const id = React.useId()
  const [values, setValues] = React.useState<UserInput>({
    first_name: user?.first_name ?? "",
    last_name: user?.last_name ?? "",
    email: user?.email ?? "",
    msid: user?.msid ?? "",
  })
  const [submitted, setSubmitted] = React.useState(false)
  const [pending, setPending] = React.useState(false)
  const [error, setError] = React.useState<ApiError>()

  const trimmed = {
    first_name: values.first_name.trim(),
    last_name: values.last_name.trim(),
    email: values.email.trim(),
    msid: values.msid.trim(),
  }
  const invalid: Partial<Record<FieldName, string>> = {
    first_name: trimmed.first_name ? undefined : "Enter their first name.",
    last_name: trimmed.last_name ? undefined : "Enter their last name.",
    email: EMAIL.test(trimmed.email)
      ? undefined
      : "Enter an email address, like ada@example.com.",
    msid: MSID.test(trimmed.msid)
      ? undefined
      : "Enter their MS ID: letters, digits, dots, dashes or underscores.",
  }
  // A taken email or MS ID comes back as 409 naming it; show it on its field.
  const taken: FieldName | undefined =
    error?.status === 409
      ? error.message.includes("MS ID")
        ? "msid"
        : "email"
      : undefined
  const errorFor = (field: FieldName) =>
    (submitted ? invalid[field] : undefined) ??
    (taken === field ? error?.message : undefined)

  const set =
    (field: FieldName) => (event: React.ChangeEvent<HTMLInputElement>) => {
      setValues((current) => ({ ...current, [field]: event.target.value }))
      if (taken === field) setError(undefined)
    }

  async function submit(event: React.SubmitEvent<HTMLFormElement>) {
    event.preventDefault()
    setSubmitted(true)
    if (Object.values(invalid).some(Boolean)) return
    setPending(true)
    setError(undefined)
    try {
      await onSubmit(trimmed, user)
      onDone()
    } catch (caught) {
      setError(toApiError(caught))
    } finally {
      setPending(false)
    }
  }

  const field = (
    name: FieldName,
    label: string,
    input: Omit<React.ComponentProps<typeof Input>, "id" | "value">,
    description?: string
  ) => {
    const message = errorFor(name)
    return (
      <Field data-invalid={Boolean(message)}>
        <FieldLabel htmlFor={`${id}-${name}`}>{label}</FieldLabel>
        <Input
          id={`${id}-${name}`}
          autoComplete="off"
          value={values[name]}
          aria-invalid={Boolean(message)}
          onChange={set(name)}
          {...input}
        />
        {message ? (
          <FieldError>{message}</FieldError>
        ) : (
          description && <FieldDescription>{description}</FieldDescription>
        )}
      </Field>
    )
  }

  return (
    <form onSubmit={submit} noValidate className="grid gap-6">
      <DialogHeader>
        <DialogTitle>{user ? "Edit user" : "New user"}</DialogTitle>
        <DialogDescription>
          {user
            ? `Change ${userName(user)}'s details. Their roles stay.`
            : "Add someone who uses Forge, so they can be given roles."}
        </DialogDescription>
      </DialogHeader>
      {error && error.status !== 409 && (
        <ErrorCallout title={`Couldn't ${user ? "save" : "add"} the user`}>
          {error.message}
        </ErrorCallout>
      )}
      <FieldGroup>
        <div className="grid grid-cols-2 gap-4 max-[600px]:grid-cols-1">
          {field("first_name", "First name", {
            autoFocus: true,
            maxLength: 100,
          })}
          {field("last_name", "Last name", { maxLength: 100 })}
        </div>
        {field(
          "email",
          "Email",
          { type: "email", maxLength: 320 },
          "Stored in lowercase; each person has their own."
        )}
        {field(
          "msid",
          "MS ID",
          { maxLength: 64, spellCheck: false, className: "font-mono" },
          "Their network user ID, e.g. jdoe. Stored in lowercase."
        )}
      </FieldGroup>
      <DialogFooter>
        <DialogClose render={<Button type="button" variant="outline" />}>
          Cancel
        </DialogClose>
        <Button type="submit" disabled={pending}>
          {pending && <Spinner data-icon="inline-start" />}
          {user ? "Save" : "Add user"}
        </Button>
      </DialogFooter>
    </form>
  )
}

type DeleteUserDialogProps = {
  /** The user to remove; the dialog is open while there is one. */
  user: User | undefined
  onClose: () => void
  /** Removes; rejects with the API's error, which the dialog shows. */
  onConfirm: (user: User) => Promise<unknown>
}

/** Confirms removing a user, which also revokes every role they hold. */
export function DeleteUserDialog({
  user,
  onClose,
  onConfirm,
}: DeleteUserDialogProps) {
  const open = user !== undefined
  const count = useOpenCount(open)
  const shown = useLastNode(user)
  return (
    <Dialog open={open} onOpenChange={(next) => !next && onClose()}>
      <DialogContent>
        {shown && (
          <DeleteUserForm
            key={count}
            user={shown}
            onConfirm={onConfirm}
            onDone={onClose}
          />
        )}
      </DialogContent>
    </Dialog>
  )
}

function DeleteUserForm({
  user,
  onConfirm,
  onDone,
}: {
  user: User
  onConfirm: (user: User) => Promise<unknown>
  onDone: () => void
}) {
  const [pending, setPending] = React.useState(false)
  const [error, setError] = React.useState<ApiError>()

  async function confirm() {
    setPending(true)
    setError(undefined)
    try {
      await onConfirm(user)
      onDone()
    } catch (caught) {
      setError(toApiError(caught))
    } finally {
      setPending(false)
    }
  }

  return (
    <>
      <DialogHeader>
        <DialogTitle>Remove {userName(user)}?</DialogTitle>
        <DialogDescription>
          This removes them from Forge and revokes every role they hold. It
          can't be undone.
        </DialogDescription>
      </DialogHeader>
      {error && (
        <ErrorCallout title="Couldn't remove the user">
          {error.message}
        </ErrorCallout>
      )}
      <DialogFooter>
        <DialogClose render={<Button variant="outline" />}>Cancel</DialogClose>
        <Button variant="destructive" disabled={pending} onClick={confirm}>
          {pending && <Spinner data-icon="inline-start" />}
          Remove user
        </Button>
      </DialogFooter>
    </>
  )
}
