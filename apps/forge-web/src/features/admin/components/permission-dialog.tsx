import * as React from "react"
import { Key01Icon } from "@hugeicons/core-free-icons"

import { ErrorCallout } from "@/components/forge/feedback"
import { Icon } from "@/components/forge/icon"
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
import { toApiError, type ApiError } from "@/lib/api/index"
import {
  GROUP_NAME_PATTERN,
  groupLinks,
  PERMISSION_KEY_PATTERN,
  type Permission,
  type PermissionInput,
} from "@/lib/access"
import { useSettingsCompanion } from "@/features/builder/components/settings-companion"
import {
  SettingsBody,
  SettingsCompanion,
  SettingsDialog,
  SettingsFooter,
  SettingsGlyph,
  SettingsHeader,
  SettingsHint,
  SettingsSection,
} from "@/features/builder/components/settings-dialog"
import { useOpenCount } from "./dialog-state"

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

/**
 * Creates or edits a permission, in the same dialog as a workflow's or an
 * agent's step settings: its `resource:action` key and description, and
 * the company groups linked to it, picked in a card beside it ("Link to
 * company groups"). Any number of groups; any of them grants it.
 */
export function PermissionDialog({
  open,
  onOpenChange,
  permission,
  ...props
}: PermissionDialogProps) {
  const count = useOpenCount(open)
  // While it closes it keeps showing what it showed: a permission, or
  // (null) a new one.
  const [shown, setShown] = React.useState<Permission | null | undefined>(
    open ? (permission ?? null) : undefined
  )
  const target = open ? (permission ?? null) : undefined
  if (target !== undefined && target !== shown) setShown(target)
  return (
    <SettingsDialog open={open} onOpenChange={onOpenChange}>
      {shown !== undefined && (
        <PermissionForm
          key={count}
          permission={shown ?? undefined}
          onDone={() => onOpenChange(false)}
          {...props}
        />
      )}
    </SettingsDialog>
  )
}

const plural = (n: number, word: string) =>
  `${n} ${n === 1 ? word : `${word}s`}`

const sameGroups = (a: string[], b: string[]) =>
  a.length === b.length && [...a].sort().join("\n") === [...b].sort().join("\n")

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
  const [groups, setGroups] = React.useState<string[]>(
    permission?.groups ?? []
  )
  const [submitted, setSubmitted] = React.useState(false)
  const [pending, setPending] = React.useState(false)
  const [error, setError] = React.useState<ApiError>()
  const [linking, setLinking] = useSettingsCompanion()

  const keyError =
    submitted && !PERMISSION_KEY_PATTERN.test(key.trim())
      ? "Use lowercase resource:action, like organizations:update."
      : error?.status === 409
        ? error.message
        : undefined
  const renaming = permission && key.trim() !== permission.key
  const dirty =
    !permission ||
    renaming ||
    description.trim() !== permission.description ||
    !sameGroups(groups, permission.groups)
  const unlink = (name: string) =>
    setGroups((current) => current.filter((group) => group !== name))

  async function submit(event: React.SubmitEvent<HTMLFormElement>) {
    event.preventDefault()
    setSubmitted(true)
    if (!PERMISSION_KEY_PATTERN.test(key.trim())) return
    setPending(true)
    setError(undefined)
    try {
      await onSubmit(
        { key: key.trim(), description: description.trim(), groups },
        permission
      )
      onDone()
    } catch (caught) {
      setError(toApiError(caught))
    } finally {
      setPending(false)
    }
  }

  const title = key.trim() || permission?.key || "New permission"
  return (
    <>
      <SettingsHeader
        glyph={<SettingsGlyph icon={Key01Icon} />}
        title={permission ? permission.key : "New permission"}
        description={`Permission · ${plural(grantedBy, "role")} · ${plural(groups.length, "company group")}`}
        closeLabel="Close the permission"
      />
      <form
        onSubmit={submit}
        noValidate
        className="flex min-h-0 flex-1 flex-col"
      >
        <SettingsBody>
          <SettingsSection className="gap-3">
            <p className="text-xs/[1.6] text-muted-foreground">
              Something people can be let do. Roles grant it (on the Roles
              page), and so do the company groups linked to it here.
            </p>
            {error && error.status !== 409 && (
              <ErrorCallout
                title={`Couldn't ${permission ? "save" : "create"} the permission`}
              >
                {error.message}
              </ErrorCallout>
            )}
          </SettingsSection>

          <SettingsSection>
            <Field data-invalid={Boolean(keyError)}>
              <FieldLabel htmlFor={`${id}-key`}>Key</FieldLabel>
              <Input
                id={`${id}-key`}
                autoFocus={!permission}
                className="font-mono text-xs"
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
                  {renaming && (grantedBy > 0 || groups.length > 0)
                    ? "The roles and company groups that have it will have the new key."
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
          </SettingsSection>

          <SettingsSection title="Company groups">
            {groups.length > 0 ? (
              <ul className="flex flex-col divide-y rounded-(--radius-item) border">
                {groups.map((name) => (
                  <GroupRow
                    key={name}
                    name={name}
                    onUnlink={() => unlink(name)}
                  />
                ))}
              </ul>
            ) : (
              <p className="text-xs/[1.6] text-muted-foreground">
                No company groups are linked: only roles grant it.
              </p>
            )}
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="self-start"
              onClick={() => setLinking(!linking)}
            >
              <Icon icon="link" data-icon="inline-start" />
              {linking ? "Hide company groups" : "Link to company groups"}
            </Button>
          </SettingsSection>
        </SettingsBody>

        <SettingsFooter>
          <SettingsHint>
            {permission
              ? dirty
                ? "Unsaved changes"
                : "No changes"
              : "Links are saved with it"}
          </SettingsHint>
          <Button
            type="button"
            variant="outline"
            size="sm"
            className="max-[600px]:ml-auto"
            onClick={onDone}
          >
            Cancel
          </Button>
          <Button type="submit" size="sm" disabled={pending || !dirty}>
            {pending && <Spinner data-icon="inline-start" />}
            {permission ? "Save" : "Create permission"}
          </Button>
        </SettingsFooter>
      </form>

      <GroupLinker
        permissionKey={title}
        groups={groups}
        onLink={(name) => setGroups((current) => [...current, name])}
        onUnlink={unlink}
        onDone={() => setLinking(false)}
      />
    </>
  )
}

/** One linked group, with unlinking it. */
function GroupRow({ name, onUnlink }: { name: string; onUnlink: () => void }) {
  return (
    <li className="flex items-center gap-2 py-1.5 pr-1.5 pl-3">
      <Icon
        icon="team"
        size={14}
        className="shrink-0 text-muted-foreground"
      />
      <span className="min-w-0 flex-1 truncate font-mono text-xs text-foreground">
        {name}
      </span>
      <Button
        type="button"
        variant="ghost"
        size="xs"
        className="text-muted-foreground hover:text-destructive"
        aria-label={`Unlink ${name}`}
        onClick={onUnlink}
      >
        <Icon icon="close" data-icon="inline-start" />
        Unlink
      </Button>
    </li>
  )
}

/**
 * The card beside the permission: link company groups by name (typed, or
 * picked from those linked to other permissions), and unlink them.
 */
function GroupLinker({
  permissionKey,
  groups,
  onLink,
  onUnlink,
  onDone,
}: {
  permissionKey: string
  groups: string[]
  onLink: (name: string) => void
  onUnlink: (name: string) => void
  onDone: () => void
}) {
  const id = React.useId()
  const known = groupLinks.useList()
  const [name, setName] = React.useState("")
  const [problem, setProblem] = React.useState<string>()
  const suggestions = (known.data ?? [])
    .map((link) => link.group_name)
    .filter((group) => !groups.includes(group))

  const add = (value: string) => {
    const trimmed = value.trim()
    if (!trimmed) return setProblem("Type the group's name.")
    if (!GROUP_NAME_PATTERN.test(trimmed))
      return setProblem(
        "Use the name as the directory spells it: letters, digits, spaces and punctuation, up to 200 characters."
      )
    if (groups.includes(trimmed))
      return setProblem(`${trimmed} is linked already.`)
    onLink(trimmed)
    setName("")
    setProblem(undefined)
  }

  return (
    <SettingsCompanion
      size="narrow"
      title="Link to company groups"
      description={
        <>
          Anyone whose sign-in says they&apos;re in one of these groups, in
          the company&apos;s directory, gets{" "}
          <code className="font-mono text-[0.9em]">{permissionKey}</code> on
          the whole site.
        </>
      }
      footer={
        <>
          <SettingsHint>Saved with the permission</SettingsHint>
          <Button
            type="button"
            size="sm"
            className="max-[600px]:ml-auto"
            onClick={onDone}
          >
            Done
          </Button>
        </>
      }
    >
      <SettingsSection>
        <Field data-invalid={Boolean(problem) || undefined}>
          <FieldLabel htmlFor={`${id}-group`}>Company group</FieldLabel>
          <div className="flex gap-2">
            <Input
              id={`${id}-group`}
              autoFocus
              maxLength={200}
              autoComplete="off"
              spellCheck={false}
              placeholder="CN=Forge Admins,OU=Groups"
              value={name}
              aria-invalid={Boolean(problem) || undefined}
              onChange={(event) => {
                setName(event.target.value)
                setProblem(undefined)
              }}
              onKeyDown={(event) => {
                if (event.key !== "Enter") return
                event.preventDefault()
                add(name)
              }}
            />
            <Button type="button" variant="outline" onClick={() => add(name)}>
              <Icon icon="plus" data-icon="inline-start" />
              Link
            </Button>
          </div>
          {problem ? (
            <FieldError>{problem}</FieldError>
          ) : (
            <FieldDescription>
              Exactly as the directory and sign-in tokens name it. Link as
              many groups as need it.
            </FieldDescription>
          )}
        </Field>
      </SettingsSection>

      <SettingsSection title={`Linked (${groups.length})`}>
        {groups.length > 0 ? (
          <ul className="flex flex-col divide-y rounded-(--radius-item) border">
            {groups.map((group) => (
              <GroupRow
                key={group}
                name={group}
                onUnlink={() => onUnlink(group)}
              />
            ))}
          </ul>
        ) : (
          <p className="text-xs/[1.6] text-muted-foreground">
            None yet: type a group above, or pick one below.
          </p>
        )}
      </SettingsSection>

      {suggestions.length > 0 && (
        <SettingsSection title="Groups linked to other permissions">
          <div className="flex flex-wrap gap-1.5">
            {suggestions.map((group) => (
              <Button
                key={group}
                type="button"
                variant="outline"
                size="xs"
                className="max-w-full"
                onClick={() => add(group)}
              >
                <Icon icon="plus" data-icon="inline-start" />
                <span className="truncate font-mono">{group}</span>
              </Button>
            ))}
          </div>
        </SettingsSection>
      )}
    </SettingsCompanion>
  )
}
