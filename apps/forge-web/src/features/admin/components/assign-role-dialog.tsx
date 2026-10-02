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
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Spinner } from "@/components/ui/spinner"
import { toApiError, type ApiError } from "@/lib/api/index"
import type { Role, RoleLevel } from "@/lib/access"
import type { Scope } from "@/lib/hierarchy"
import { useOpenCount } from "./dialog-state"
import { UserPicker } from "./user-picker"

/** A scope roles can be assigned in: the site or an organization. */
export type AssignTarget = { scope: Scope; label: string; level: RoleLevel }

type AssignRoleDialogProps = {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** Where roles can be assigned from here; the first is the default. */
  targets: AssignTarget[]
  /** Start with this one picked instead of the first, e.g. an organization's. */
  defaultScope?: Scope
  title?: string
  description?: string
  /** The submit button's label. */
  submitLabel?: string
  /** Every role; those of the chosen scope's level are offered. */
  roles: Role[]
  /** Assigns; rejects with the API's error, which the dialog shows. */
  onSubmit: (input: {
    scope: Scope
    role: string
    subjectIds: string[]
  }) => Promise<unknown>
}

/** Gives one or more people a role in the scope or one of those inside it. */
export function AssignRoleDialog({
  open,
  onOpenChange,
  ...props
}: AssignRoleDialogProps) {
  const count = useOpenCount(open)
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <AssignForm key={count} onDone={() => onOpenChange(false)} {...props} />
      </DialogContent>
    </Dialog>
  )
}

function AssignForm({
  targets,
  defaultScope,
  title = "Assign a role",
  submitLabel = "Assign role",
  description = "People get what the role grants where you assign it; a site role applies in every organization.",
  roles,
  onSubmit,
  onDone,
}: Omit<AssignRoleDialogProps, "open" | "onOpenChange"> & {
  onDone: () => void
}) {
  const id = React.useId()
  const [people, setPeople] = React.useState<string[]>([])
  const [scope, setScope] = React.useState<Scope | undefined>(
    targets.some((t) => t.scope === defaultScope)
      ? defaultScope
      : targets[0]?.scope
  )
  const [roleKey, setRoleKey] = React.useState<string>()
  const [submitted, setSubmitted] = React.useState(false)
  const [pending, setPending] = React.useState(false)
  const [error, setError] = React.useState<ApiError>()

  const target = targets.find((t) => t.scope === scope)
  const offered = roles.filter((role) => role.level === target?.level)
  const role = offered.find((r) => r.key === roleKey)
  const scopeItems = targets.map((t) => ({ value: t.scope, label: t.label }))
  const roleItems = offered.map((r) => ({ value: r.key, label: r.name }))

  const peopleError =
    submitted && people.length === 0 ? "Pick at least one person." : undefined
  const roleError = submitted && !role ? "Pick a role." : undefined

  async function submit(event: React.SubmitEvent<HTMLFormElement>) {
    event.preventDefault()
    setSubmitted(true)
    if (people.length === 0 || !scope || !role) return
    setPending(true)
    setError(undefined)
    try {
      await onSubmit({ scope, role: role.key, subjectIds: people })
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
        <DialogTitle>{title}</DialogTitle>
        <DialogDescription>{description}</DialogDescription>
      </DialogHeader>
      {error && (
        <ErrorCallout title="Couldn't assign the role">
          {error.message}
        </ErrorCallout>
      )}
      <FieldGroup>
        <Field data-invalid={Boolean(peopleError)}>
          <FieldLabel htmlFor={`${id}-people`}>People</FieldLabel>
          <UserPicker
            id={`${id}-people`}
            value={people}
            onValueChange={setPeople}
          />
          {peopleError && <FieldError>{peopleError}</FieldError>}
        </Field>
        <Field>
          <FieldLabel htmlFor={`${id}-scope`}>Where</FieldLabel>
          <Select
            items={scopeItems}
            value={scope}
            onValueChange={(value) => {
              if (!value) return
              setScope(value)
              // Roles belong to a level; keep the pick only if it still fits.
              const next = targets.find((t) => t.scope === value)
              if (next?.level !== target?.level) setRoleKey(undefined)
            }}
          >
            <SelectTrigger id={`${id}-scope`} className="w-full">
              <SelectValue />
            </SelectTrigger>
            <SelectContent alignItemWithTrigger={false}>
              {scopeItems.map((item) => (
                <SelectItem key={item.value} value={item.value}>
                  {item.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </Field>
        <Field data-invalid={Boolean(roleError)}>
          <FieldLabel htmlFor={`${id}-role`}>Role</FieldLabel>
          <Select
            items={roleItems}
            value={roleKey ?? null}
            onValueChange={(value) => value && setRoleKey(value)}
          >
            <SelectTrigger
              id={`${id}-role`}
              className="w-full"
              aria-invalid={Boolean(roleError)}
            >
              <SelectValue placeholder="Pick a role" />
            </SelectTrigger>
            <SelectContent alignItemWithTrigger={false}>
              {roleItems.map((item) => (
                <SelectItem key={item.value} value={item.value}>
                  {item.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          {roleError ? (
            <FieldError>{roleError}</FieldError>
          ) : role ? (
            <FieldDescription>
              {role.description || role.name} Grants{" "}
              {role.permissions.length === 1
                ? "1 permission"
                : `${role.permissions.length} permissions`}
              .
            </FieldDescription>
          ) : offered.length === 0 ? (
            <FieldDescription>
              No roles are assigned at this level. Create one on the Roles page.
            </FieldDescription>
          ) : null}
        </Field>
      </FieldGroup>
      <DialogFooter>
        <DialogClose render={<Button type="button" variant="outline" />}>
          Cancel
        </DialogClose>
        <Button type="submit" disabled={pending}>
          {pending && <Spinner data-icon="inline-start" />}
          {submitLabel}
        </Button>
      </DialogFooter>
    </form>
  )
}
