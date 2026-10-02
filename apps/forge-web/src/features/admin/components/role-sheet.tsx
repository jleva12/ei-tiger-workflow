import * as React from "react"
import { UserShield01Icon } from "@hugeicons/core-free-icons"

import { ErrorCallout } from "@/components/forge/feedback"
import {
  FormColumns,
  FormFooter,
  TaskForm,
  TaskSheetContent,
  TaskSheetHeader,
} from "@/components/forge/task-sheet"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import {
  Field,
  FieldContent,
  FieldDescription,
  FieldError,
  FieldGroup,
  FieldLabel,
  FieldLegend,
  FieldSet,
} from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import {
  InputGroup,
  InputGroupAddon,
  InputGroupInput,
  InputGroupText,
} from "@/components/ui/input-group"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Sheet } from "@/components/ui/sheet"
import { Skeleton } from "@/components/ui/skeleton"
import { Spinner } from "@/components/ui/spinner"
import { Textarea } from "@/components/ui/textarea"
import { toApiError, type ApiError } from "@/lib/api/index"
import {
  levelLabel,
  permissions as permissionsResource,
  ROLE_LEVELS,
  ROLE_NAME_PATTERN,
  type Permission,
  type Role,
  type RoleCreate,
  type RoleLevel,
  type RoleUpdate,
} from "@/lib/access"
import { useLastNode, useOpenCount } from "./dialog-state"

type RoleSheetProps = {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** The role to edit or view; without one the sheet creates a role. */
  role?: Role
  /** Show the role without letting it be changed. */
  readOnly?: boolean
  /** Creates (without `role`); rejects with the API's error, which it shows. */
  onCreate?: (input: RoleCreate) => Promise<unknown>
  /** Saves `role`; rejects with the API's error, which it shows. */
  onUpdate?: (key: string, input: RoleUpdate) => Promise<unknown>
}

/** Creates a role, or shows and edits one: its name and what it grants. */
export function RoleSheet({
  open,
  onOpenChange,
  role,
  ...props
}: RoleSheetProps) {
  const count = useOpenCount(open)
  const shown = useLastNode(role)
  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <TaskSheetContent>
        <RoleForm
          key={count}
          role={shown}
          onDone={() => onOpenChange(false)}
          {...props}
        />
      </TaskSheetContent>
    </Sheet>
  )
}

/** "Release manager" → "release_manager". */
const toKeyName = (name: string) =>
  name
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^[^a-z]+|_+$/g, "")

function RoleForm({
  role,
  readOnly = false,
  onCreate,
  onUpdate,
  onDone,
}: Omit<RoleSheetProps, "open" | "onOpenChange"> & { onDone: () => void }) {
  const id = React.useId()
  const [level, setLevel] = React.useState<RoleLevel>(role?.level ?? "org")
  const [keyName, setKeyName] = React.useState("")
  const [keyEdited, setKeyEdited] = React.useState(false)
  const [name, setName] = React.useState(role?.name ?? "")
  const [description, setDescription] = React.useState(role?.description ?? "")
  const [granted, setGranted] = React.useState<number[]>(
    role?.permissions.map((p) => p.id) ?? []
  )
  const [submitted, setSubmitted] = React.useState(false)
  const [pending, setPending] = React.useState(false)
  const [error, setError] = React.useState<ApiError>()

  // Until the key is typed, it follows the name.
  const suffix = keyEdited ? keyName : toKeyName(name)
  const nameError =
    submitted && !name.trim() ? "Give the role a name." : undefined
  const keyError = role
    ? undefined
    : submitted && !ROLE_NAME_PATTERN.test(suffix)
      ? "Use lowercase letters, digits and _, starting with a letter."
      : error?.status === 409
        ? error.message
        : undefined

  async function submit(event: React.SubmitEvent<HTMLFormElement>) {
    event.preventDefault()
    setSubmitted(true)
    if (!name.trim() || (!role && !ROLE_NAME_PATTERN.test(suffix))) return
    setPending(true)
    setError(undefined)
    const fields = {
      name: name.trim(),
      description: description.trim(),
      permission_ids: granted,
    }
    try {
      if (role) await onUpdate?.(role.key, fields)
      else await onCreate?.({ key: `${level}:${suffix}`, ...fields })
      onDone()
    } catch (caught) {
      setError(toApiError(caught))
    } finally {
      setPending(false)
    }
  }

  return (
    <>
      <TaskSheetHeader
        eyebrow={role ? role.key : "Roles"}
        eyebrowIcon={UserShield01Icon}
        title={role ? role.name : "New role"}
        description={
          role
            ? `Assigned at the ${levelLabel(role.level).toLowerCase()} level; applies there${role.level === "site" ? " and in every organization" : ""}. ${role.member_count === 0 ? "Nobody holds it yet." : `Assigned ${role.member_count === 1 ? "once" : `${role.member_count} times`}.`}`
            : "A set of permissions to give people in one kind of scope."
        }
      />
      <TaskForm onSubmit={submit} noValidate>
        {error && error.status !== 409 && (
          <ErrorCallout title={`Couldn't ${role ? "save" : "create"} the role`}>
            {error.message}
          </ErrorCallout>
        )}
        <FieldGroup>
          <Field data-invalid={Boolean(nameError)}>
            <FieldLabel htmlFor={`${id}-name`}>Name</FieldLabel>
            <Input
              id={`${id}-name`}
              autoFocus={!readOnly}
              maxLength={200}
              autoComplete="off"
              disabled={readOnly}
              value={name}
              aria-invalid={Boolean(nameError)}
              onChange={(event) => setName(event.target.value)}
            />
            {nameError && <FieldError>{nameError}</FieldError>}
          </Field>
          {!role && (
            <FormColumns>
              <Field>
                <FieldLabel htmlFor={`${id}-level`}>Assigned in</FieldLabel>
                <Select
                  items={ROLE_LEVELS}
                  value={level}
                  onValueChange={(value) => value && setLevel(value)}
                >
                  <SelectTrigger id={`${id}-level`}>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent alignItemWithTrigger={false}>
                    {ROLE_LEVELS.map((option) => (
                      <SelectItem key={option.value} value={option.value}>
                        {option.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </Field>
              <Field data-invalid={Boolean(keyError)}>
                <FieldLabel htmlFor={`${id}-key`}>Key</FieldLabel>
                <InputGroup className="h-9">
                  <InputGroupAddon>
                    <InputGroupText className="font-mono text-xs">
                      {level}:
                    </InputGroupText>
                  </InputGroupAddon>
                  <InputGroupInput
                    id={`${id}-key`}
                    className="font-mono text-xs"
                    maxLength={90}
                    autoComplete="off"
                    spellCheck={false}
                    value={suffix}
                    aria-invalid={Boolean(keyError)}
                    onChange={(event) => {
                      setKeyEdited(true)
                      setKeyName(event.target.value)
                      if (error?.status === 409) setError(undefined)
                    }}
                  />
                </InputGroup>
              </Field>
            </FormColumns>
          )}
          {keyError ? (
            <FieldError className="-mt-3">{keyError}</FieldError>
          ) : (
            !role && (
              <FieldDescription className="-mt-3">
                It applies where it's assigned (a site role, in every
                organization too). The key can't change later.
              </FieldDescription>
            )
          )}
          <Field>
            <FieldLabel htmlFor={`${id}-description`}>Description</FieldLabel>
            <Textarea
              id={`${id}-description`}
              className="min-h-20!"
              disabled={readOnly}
              value={description}
              onChange={(event) => setDescription(event.target.value)}
            />
          </Field>
          <PermissionChecklist
            value={granted}
            onValueChange={setGranted}
            disabled={readOnly}
          />
        </FieldGroup>
        <FormFooter
          hint={`${granted.length} ${granted.length === 1 ? "permission" : "permissions"}`}
          hintIcon="check"
        >
          {readOnly ? (
            <Button type="button" variant="outline" onClick={onDone}>
              Close
            </Button>
          ) : (
            <div className="flex gap-2">
              <Button type="button" variant="outline" onClick={onDone}>
                Cancel
              </Button>
              <Button type="submit" disabled={pending}>
                {pending && <Spinner data-icon="inline-start" />}
                {role ? "Save role" : "Create role"}
              </Button>
            </div>
          )}
        </FormFooter>
      </TaskForm>
    </>
  )
}

/** Every permission as a checkbox, grouped by resource. */
function PermissionChecklist({
  value,
  onValueChange,
  disabled,
}: {
  value: number[]
  onValueChange: (value: number[]) => void
  disabled: boolean
}) {
  const id = React.useId()
  const list = permissionsResource.useList()
  const groups = React.useMemo(() => {
    const byResource = new Map<string, Permission[]>()
    for (const permission of list.data ?? []) {
      const group = byResource.get(permission.resource) ?? []
      group.push(permission)
      byResource.set(permission.resource, group)
    }
    return [...byResource]
  }, [list.data])

  const toggle = (permissionId: number, on: boolean) =>
    onValueChange(
      on
        ? [...value, permissionId]
        : value.filter((other) => other !== permissionId)
    )

  return (
    <FieldSet>
      <FieldLegend variant="label">Permissions</FieldLegend>
      <FieldDescription>What people with this role may do.</FieldDescription>
      {list.isPending ? (
        <div className="flex flex-col gap-2" aria-busy="true">
          <Skeleton className="h-8" />
          <Skeleton className="h-8" />
          <Skeleton className="h-8" />
        </div>
      ) : list.error ? (
        <ErrorCallout
          title="Couldn't load the permissions"
          action={
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => void list.refetch()}
            >
              Retry
            </Button>
          }
        >
          {list.error.message}
        </ErrorCallout>
      ) : (
        <div className="flex flex-col gap-4">
          {groups.map(([resource, group]) => (
            <div key={resource} className="flex flex-col gap-2.5">
              <h3 className="text-3xs tracking-[.09em] text-subtle uppercase">
                {resource === "*" ? "Every resource" : resource}
              </h3>
              {group.map((permission) => {
                const checkboxId = `${id}-${permission.id}`
                return (
                  <Field key={permission.id} orientation="horizontal">
                    <Checkbox
                      id={checkboxId}
                      disabled={disabled}
                      checked={value.includes(permission.id)}
                      onCheckedChange={(on) => toggle(permission.id, on)}
                    />
                    <FieldContent>
                      <FieldLabel
                        htmlFor={checkboxId}
                        className="font-mono font-normal"
                      >
                        {permission.key}
                      </FieldLabel>
                      {permission.description && (
                        <FieldDescription>
                          {permission.description}
                        </FieldDescription>
                      )}
                    </FieldContent>
                  </Field>
                )
              })}
            </div>
          ))}
        </div>
      )}
    </FieldSet>
  )
}
