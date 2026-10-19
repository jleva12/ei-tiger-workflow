import * as React from "react"
import { UserShield01Icon } from "@hugeicons/core-free-icons"
import { cn } from "cn"

import { PermissionDialog } from "@/features/admin/components/permission-dialog"
import { IssueMessages } from "@/features/builder/components/fields/field-issues"
import { useFieldIssues } from "@/features/builder/components/fields/field-issues-context"
import { useCompanionCard } from "@/features/builder/components/settings-companion"
import { SettingsCompanion } from "@/features/builder/components/settings-dialog"
import {
  agentPermissions,
  useAgentGroups,
  type AgentPermission,
} from "@/features/agent-access/lib/api"
import { organizationApiKeys } from "@/features/api-keys/lib/api"
import { useMyOrganizations } from "@/lib/access"
import { useScopeAccess } from "@/lib/hierarchy"
import {
  draftId,
  draftPermissionCount,
  emptyGroup,
  fromDraft,
  MAX_RULE_DEPTH,
  MAX_RULE_PERMISSIONS,
  ruleWords,
  toDraft,
  type AccessRule,
  type DraftGroup,
  type DraftItem,
  type RuleMode,
} from "@/features/agents/lib/access"
import { Icon } from "@/components/forge/icon"
import { Button } from "@/components/ui/button"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Skeleton } from "@/components/ui/skeleton"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"

/*
 * Who may use an agent on the canvas: the rule on its node, which the
 * runtime checks for whoever the call is for each time the agent starts.
 * The node's settings show the rule; it's edited in a card beside them,
 * as groups of the organization's agent permissions (Settings → Agent
 * access) that all, or any, must hold, nesting. Those who manage them
 * (`agent_access:manage`) make new ones right there, in the same dialog as
 * the settings page's, and the new one goes into the rule.
 */

const MODES: { value: RuleMode; label: string }[] = [
  { value: "any", label: "Any of these" },
  { value: "all", label: "All of these" },
]

/** The organization's agent permissions, and how to name one by its ID. */
function usePermissions(organizationId: string) {
  // Reading them takes members:read: without it, rules show IDs.
  const list = agentPermissions
    .scope({ organizationId })
    .useList(undefined, { retry: false })
  const known = list.data
  const byId = React.useMemo(
    () => new Map((known ?? []).map((p) => [p.id, p])),
    [known]
  )
  const name = React.useCallback(
    (id: number) => (known ? byId.get(id)?.key : `permission #${id}`),
    [known, byId]
  )
  return { list, known, byId, name }
}

/**
 * An agent's access rule in its settings: what it needs, and a button
 * opening the rule's editor.
 *
 * @param label What the agent is called on the canvas.
 */
export function AccessRuleField({
  organizationId,
  label,
  rule,
  onChange,
}: {
  organizationId: string
  label: string
  rule: AccessRule | null
  onChange: (rule: AccessRule | null) => void
}) {
  const { issues, key: field } = useFieldIssues("access")
  const key = React.useId()
  const editor = useCompanionCard(key)
  const permissions = usePermissions(organizationId)
  const [draft, setDraft] = React.useState<DraftGroup>(emptyGroup)

  const edit = () => {
    setDraft(toDraft(rule))
    editor.show()
  }
  const saved = fromDraft(draft)

  return (
    <div
      role="group"
      aria-label="Who may use it"
      data-field={field}
      className="flex flex-col gap-2"
    >
      <div className="flex items-center justify-between gap-2">
        <span className="text-xs font-medium text-foreground">
          Who may use it
        </span>
        <Button
          type="button"
          variant="outline"
          size="xs"
          aria-expanded={editor.open}
          disabled={!editor.canOpen}
          onClick={edit}
        >
          <Icon icon={UserShield01Icon} data-icon="inline-start" />
          {rule ? "Edit permissions" : "Add permissions"}
        </Button>
      </div>
      {rule ? (
        <>
          <RuleTree rule={rule} name={permissions.name} />
          <p className="text-xs/[1.6] text-muted-foreground">
            Anyone else is told what it needs instead. Those who manage the
            organization&apos;s agents may always use it.
          </p>
        </>
      ) : (
        <p className="text-xs/[1.6] text-muted-foreground">
          Anyone who may call the agent. Add permissions to let in only those
          who hold them; the rest are told what it needs.
        </p>
      )}
      <IssueMessages issues={issues} />

      {/* Gone while open (its node's settings changed), it closes too. */}
      <SettingsCompanion
        id={key}
        icon={UserShield01Icon}
        title="Who may use it"
        description={
          <>
            Which of the organization&apos;s agent permissions someone needs to
            use {label}. It&apos;s checked each time the agent starts, for
            whoever the chat is for: someone who doesn&apos;t hold them is told
            what it needs, and the conversation stays with the agent that handed
            off.
          </>
        }
        size="sheet"
        closeLabel="Close without saving"
        render={
          <form
            onSubmit={(event) => {
              event.preventDefault()
              onChange(saved)
              editor.hide()
            }}
          />
        }
        bodyClassName="flex flex-col gap-5 px-5 py-4"
        footer={
          <>
            <span className="flex items-center gap-1.5 text-2xs text-subtle max-[600px]:hidden">
              <Icon icon={UserShield01Icon} size={14} />
              Nothing changes until you save
            </span>
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="ml-auto"
              onClick={() => editor.hide()}
            >
              Cancel
            </Button>
            <Button type="submit" size="sm">
              Save access
            </Button>
          </>
        }
      >
        <RuleEditor
          organizationId={organizationId}
          permissions={permissions}
          draft={draft}
          onChange={setDraft}
        />
        <section className="flex flex-col gap-1.5">
          <h3 className="text-xs font-medium text-foreground">In words</h3>
          <p className="rounded-(--radius-control) border bg-muted/40 px-3 py-2 text-xs/[1.6] text-foreground">
            {saved
              ? `${label} needs ${ruleWords(saved, permissions.name)}.`
              : `Anyone who may call ${label} may use it.`}
          </p>
          <p className="text-2xs/[1.5] text-muted-foreground">
            Permissions not picked yet and empty groups are left out when you
            save. Those who manage the organization&apos;s agents may always use
            it.
          </p>
        </section>
      </SettingsCompanion>
    </div>
  )
}

/** A rule, read-only: its groups and permissions as a tree. */
function RuleTree({
  rule,
  name,
}: {
  rule: AccessRule
  name: (id: number) => string | undefined
}) {
  const group =
    "permission" in rule ? { mode: "any" as const, items: [rule] } : null
  const mode: RuleMode = group ? "any" : "all" in rule ? "all" : "any"
  const items = group
    ? group.items
    : "all" in rule
      ? rule.all
      : "any" in rule
        ? rule.any
        : []
  return (
    <div className="flex flex-col rounded-(--radius-control) border">
      <span className="px-2.5 py-1.5 text-2xs font-medium text-muted-foreground">
        {items.length === 1
          ? "Needs"
          : mode === "all"
            ? "Needs all of"
            : "Needs any of"}
      </span>
      <ul className="flex flex-col">
        {items.map((item, index) => (
          <RuleTreeItem key={index} rule={item} name={name} />
        ))}
      </ul>
    </div>
  )
}

function RuleTreeItem({
  rule,
  name,
}: {
  rule: AccessRule
  name: (id: number) => string | undefined
}) {
  if ("permission" in rule) {
    const known = name(rule.permission)
    return (
      <li className="flex items-center gap-2 border-t px-2.5 py-1.5">
        <Icon
          icon={UserShield01Icon}
          size={12}
          className="shrink-0 text-subtle"
        />
        {known ? (
          <code className="min-w-0 truncate font-mono text-xs text-foreground">
            {known}
          </code>
        ) : (
          <span className="text-xs text-destructive">
            A deleted permission: nobody holds it
          </span>
        )}
      </li>
    )
  }
  const every = "all" in rule
  const items = every ? rule.all : rule.any
  return (
    <li className="flex flex-col border-t py-1.5 pl-2.5">
      <span className="pb-1 text-2xs font-medium text-muted-foreground">
        {every ? "All of" : "Any of"}
      </span>
      <ul className="flex flex-col border-l">
        {items.map((item, index) => (
          <RuleTreeItem key={index} rule={item} name={name} />
        ))}
      </ul>
    </li>
  )
}

/* -------------------------------------------------------------------------- */
/* Editing                                                                    */
/* -------------------------------------------------------------------------- */

type Permissions = ReturnType<typeof usePermissions>

/** The draft with one of its items (or groups) changed. */
function changed(
  group: DraftGroup,
  id: string,
  change: (item: DraftItem) => DraftItem | null
): DraftGroup {
  const items: DraftItem[] = []
  for (const item of group.items) {
    if (item.id === id) {
      const next = change(item)
      if (next) items.push(next)
    } else if (item.type === "group") {
      items.push(changed(item, id, change))
    } else {
      items.push(item)
    }
  }
  return { ...group, items }
}

/** The draft with a permission added to one of its groups. */
function addedTo(
  group: DraftGroup,
  id: string,
  permission: number
): DraftGroup {
  if (group.id === id)
    return {
      ...group,
      items: [
        ...group.items,
        { id: draftId(), type: "permission", permission },
      ],
    }
  return {
    ...group,
    items: group.items.map((item) =>
      item.type === "group" ? addedTo(item, id, permission) : item
    ),
  }
}

/** The rule's editor: the root group, and what's in it. */
function RuleEditor({
  organizationId,
  permissions,
  draft,
  onChange,
}: {
  organizationId: string
  permissions: Permissions
  draft: DraftGroup
  onChange: React.Dispatch<React.SetStateAction<DraftGroup>>
}) {
  const { list, known } = permissions
  const can = useScopeAccess(`org:${organizationId}`)
  const canCreate = can("agent_access:manage")
  // The group a new permission goes into, while it's being made.
  const [into, setInto] = React.useState<string | null>(null)
  const count = draftPermissionCount(draft)
  const full = count >= MAX_RULE_PERMISSIONS
  const onNew = canCreate && !full ? setInto : undefined

  let body: React.ReactNode
  if (list.isPending)
    body = (
      <div className="flex flex-col gap-2" aria-busy="true">
        <Skeleton className="h-8" />
        <Skeleton className="h-8" />
      </div>
    )
  else if (list.error)
    body = (
      <p className="text-xs/[1.6] text-muted-foreground">
        {list.error.status === 403
          ? "You can't see the organization's agent permissions, so you can't pick them: ask someone who manages the organization's members."
          : `Couldn't load the agent permissions: ${list.error.message}`}
      </p>
    )
  else if (!known?.length && !draft.items.length)
    body = (
      <div className="flex flex-col items-start gap-2.5 rounded-(--radius-control) border border-dashed px-3 py-3">
        <p className="text-xs/[1.6] text-muted-foreground">
          The organization has no agent permissions yet. Each is something its
          agents can ask for, held by the company groups (or API keys) linked to
          it.
          {canCreate
            ? ""
            : " Ask someone who manages the organization's agent access to make one."}
        </p>
        {onNew && (
          <Button
            type="button"
            variant="outline"
            size="xs"
            onClick={() => onNew(draft.id)}
          >
            <Icon icon="plus" data-icon="inline-start" />
            New permission
          </Button>
        )}
      </div>
    )
  else
    body = (
      <section className="flex flex-col gap-2">
        <GroupEditor
          group={draft}
          depth={1}
          root
          permissions={permissions}
          full={full}
          onChange={(next) => onChange(next)}
          onItem={(id, change) =>
            onChange((current) => changed(current, id, change))
          }
          onNew={onNew}
        />
        {full && (
          <p className="text-2xs/[1.5] text-muted-foreground">
            A rule names at most {MAX_RULE_PERMISSIONS} permissions.
          </p>
        )}
      </section>
    )
  return (
    <>
      {body}
      {canCreate && (
        // Its form is its own: React bubbles a submit up its tree, through
        // the portal, and the rule's form around it would save and close.
        <div className="contents" onSubmit={(event) => event.stopPropagation()}>
          <NewPermissionDialog
            organizationId={organizationId}
            open={into !== null}
            onOpenChange={(open) => !open && setInto(null)}
            onCreated={(made) => {
              const group = into
              if (group !== null)
                onChange((current) => addedTo(current, group, made.id))
            }}
          />
        </div>
      )}
    </>
  )
}

/**
 * A new agent permission, made without leaving the agent: the settings
 * page's dialog (its key, description, company groups and API keys).
 */
function NewPermissionDialog({
  organizationId,
  open,
  onOpenChange,
  onCreated,
}: {
  organizationId: string
  open: boolean
  onOpenChange: (open: boolean) => void
  onCreated: (made: AgentPermission) => void
}) {
  // The dialog shows failures itself.
  const create = agentPermissions
    .scope({ organizationId })
    .useCreate({ meta: { silent: true } })
  const organization = useMyOrganizations().data?.find(
    (o) => o.id === organizationId
  )
  const groups = useAgentGroups(organizationId)
  const can = useScopeAccess(`org:${organizationId}`)
  const keys = organizationApiKeys
    .scope({ organizationId })
    .useList(undefined, { enabled: open && can("api_keys:manage") })
  return (
    <PermissionDialog
      open={open}
      onOpenChange={onOpenChange}
      scheme={{
        where: organization?.name ?? "the organization",
        knownGroups: (groups.data ?? []).map((link) => link.group_name),
        example: "invoices:approve",
        apiKeys: (keys.data ?? []).map((key) => ({
          id: key.id,
          name: key.name,
        })),
      }}
      onSubmit={async (input) => {
        const made = await create.mutateAsync(input)
        onCreated(made)
        return made
      }}
    />
  )
}

/**
 * A group: whether all or any of its rules must hold, its permissions and
 * the groups in it, and adding more.
 *
 * @param depth How deep it is: 1 for the root.
 */
function GroupEditor({
  group,
  depth,
  root,
  permissions,
  full,
  onChange,
  onItem,
  onNew,
}: {
  group: DraftGroup
  depth: number
  root?: boolean
  permissions: Permissions
  /** The rule names as many permissions as it may. */
  full: boolean
  onChange: (group: DraftGroup) => void
  onItem: (id: string, change: (item: DraftItem) => DraftItem | null) => void
  /** Makes a new agent permission for a group: theirs who may make them. */
  onNew?: (groupId: string) => void
}) {
  const labelId = React.useId()
  const deepest = depth >= MAX_RULE_DEPTH
  const add = (item: DraftItem) =>
    onChange({ ...group, items: [...group.items, item] })
  return (
    <div
      role="group"
      aria-labelledby={labelId}
      className={cn(
        "flex flex-col gap-2",
        !root && "rounded-(--radius-control) border bg-muted/30 p-2.5"
      )}
    >
      <div className="flex items-center gap-2">
        <span id={labelId} className="shrink-0 text-xs text-muted-foreground">
          {root ? "Someone may use it when they hold" : "They hold"}
        </span>
        <ToggleGroup
          aria-labelledby={labelId}
          value={[group.mode]}
          onValueChange={(next) => {
            if (next[0]) onChange({ ...group, mode: next[0] as RuleMode })
          }}
          spacing={0}
          className="flex rounded-(--radius-control) bg-muted p-[3px]"
        >
          {MODES.map((mode) => (
            <ToggleGroupItem
              key={mode.value}
              value={mode.value}
              className="h-[24px] min-w-0 rounded-(--radius-soft)! px-2 text-xs font-normal text-muted-foreground hover:bg-transparent hover:text-foreground aria-pressed:bg-background aria-pressed:text-foreground aria-pressed:shadow-(--shadow-raised)"
            >
              {mode.label}
            </ToggleGroupItem>
          ))}
        </ToggleGroup>
        {!root && (
          <Button
            type="button"
            variant="ghost"
            size="icon-xs"
            className="ml-auto"
            aria-label="Remove this group"
            onClick={() => onItem(group.id, () => null)}
          >
            <Icon icon="close" />
          </Button>
        )}
      </div>
      {group.items.length > 0 && (
        <ul className="flex flex-col gap-2">
          {group.items.map((item) => (
            <li key={item.id}>
              {item.type === "permission" ? (
                <PermissionRow
                  item={item}
                  permissions={permissions}
                  onChange={(permission) =>
                    onItem(item.id, () => ({ ...item, permission }))
                  }
                  onRemove={() => onItem(item.id, () => null)}
                />
              ) : (
                <GroupEditor
                  group={item}
                  depth={depth + 1}
                  permissions={permissions}
                  full={full}
                  onChange={(next) => onItem(item.id, () => next)}
                  onItem={onItem}
                  onNew={onNew}
                />
              )}
            </li>
          ))}
        </ul>
      )}
      {!group.items.length && root && (
        <p className="text-xs/[1.6] text-muted-foreground">
          No permissions yet: anyone who may call the agent may use it.
        </p>
      )}
      <div className="flex flex-wrap gap-1.5">
        <Button
          type="button"
          variant="outline"
          size="xs"
          disabled={full}
          onClick={() =>
            add({ id: draftId(), type: "permission", permission: null })
          }
        >
          <Icon icon="plus" data-icon="inline-start" />
          Permission
        </Button>
        <Button
          type="button"
          variant="outline"
          size="xs"
          disabled={deepest || full}
          title={
            deepest
              ? `Groups nest at most ${MAX_RULE_DEPTH} deep`
              : "A group of permissions that all, or any, must hold"
          }
          onClick={() =>
            add({
              ...emptyGroup(group.mode === "any" ? "all" : "any"),
              items: [{ id: draftId(), type: "permission", permission: null }],
            })
          }
        >
          <Icon icon="plus" data-icon="inline-start" />
          Group
        </Button>
        {onNew && (
          <Button
            type="button"
            variant="ghost"
            size="xs"
            className="ml-auto"
            title="Make a new agent permission, and add it here"
            onClick={() => onNew(group.id)}
          >
            <Icon icon="plus" data-icon="inline-start" />
            New permission
          </Button>
        )}
      </div>
    </div>
  )
}

/** One permission in a group: picked from the organization's. */
function PermissionRow({
  item,
  permissions,
  onChange,
  onRemove,
}: {
  item: { permission: number | null }
  permissions: Permissions
  onChange: (permission: number) => void
  onRemove: () => void
}) {
  const { known, byId } = permissions
  const options: { value: string; label: string }[] = (known ?? []).map(
    (p: AgentPermission) => ({ value: String(p.id), label: p.key })
  )
  // Picked once, deleted since: still shown, so it can be swapped or removed.
  const deleted = item.permission !== null && !byId.has(item.permission)
  if (deleted)
    options.push({
      value: String(item.permission),
      label: "A deleted permission",
    })
  return (
    <div className="flex items-center gap-1.5">
      <Select
        items={options}
        value={item.permission === null ? null : String(item.permission)}
        onValueChange={(next) => {
          if (next) onChange(Number(next))
        }}
      >
        <SelectTrigger
          aria-label="Permission"
          aria-invalid={deleted || undefined}
          className="h-8 min-w-0 flex-1 font-mono text-xs"
        >
          <SelectValue placeholder="Pick a permission" />
        </SelectTrigger>
        <SelectContent alignItemWithTrigger={false}>
          {(known ?? []).map((permission) => (
            <SelectItem key={permission.id} value={String(permission.id)}>
              <span className="flex min-w-0 flex-col">
                <span className="font-mono text-xs">{permission.key}</span>
                {permission.description && (
                  <span className="truncate text-2xs text-muted-foreground">
                    {permission.description}
                  </span>
                )}
              </span>
            </SelectItem>
          ))}
          {deleted && (
            <SelectItem value={String(item.permission)}>
              A deleted permission
            </SelectItem>
          )}
        </SelectContent>
      </Select>
      <Button
        type="button"
        variant="ghost"
        size="icon-xs"
        aria-label="Remove this permission"
        onClick={onRemove}
      >
        <Icon icon="close" />
      </Button>
    </div>
  )
}
