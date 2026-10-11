import * as React from "react"

import { ErrorCallout } from "@/components/forge/feedback"
import { Icon } from "@/components/forge/icon"
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
  SelectGroup,
  SelectItem,
  SelectLabel,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Spinner } from "@/components/ui/spinner"
import { Textarea } from "@/components/ui/textarea"
import { toast } from "@/components/ui/toast"
import { useOpenCount } from "@/features/admin/components/dialog-state"
import type { KnowledgeScope } from "../lib/api"
import {
  COMPONENT_KIND_ITEMS,
  COMPONENT_KINDS,
  CONNECTION_KIND_ITEMS,
  CONNECTION_KINDS,
  endFields,
  components as componentsResource,
  connections as connectionsResource,
  groups as groupsResource,
  useDesignRefresh,
  type Component,
  type ComponentKind,
  type Connection,
  type ConnectionKind,
  type Group,
  type MapEnd,
} from "../lib/system"
import {
  endFrom,
  endValue,
  type Editing,
  type Part,
  type Parts,
} from "../lib/map-parts"
import { GroupPicker, PartLead } from "./system-map-parts"

/*
 * The system map's dialogs: connecting two things, adding or changing a
 * component, and adding or changing a group.
 */

/** The kind of connection usually drawn between two ends. */
function suggestedKind(
  source: Part | undefined,
  target: Part | undefined
): ConnectionKind {
  if (target?.type === "component")
    return COMPONENT_KINDS[target.component.kind].connection
  if (source?.type === "component" && source.component.kind === "queue")
    return "events"
  return "calls"
}

/**
 * Connects one of the knowledge base's applications or components to
 * another: from, how and to, read as a sentence ("orders-api writes to
 * orders-db"), with an optional note. The connection suggests itself from
 * what the ends are, until one's picked.
 */
export function ConnectDialog({
  open,
  onOpenChange,
  ...props
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  scope: KnowledgeScope
  parts: Parts
  /** What to connect from, when something's selected. */
  source: MapEnd | undefined
  onConnected: (connection: Connection) => void
}) {
  const count = useOpenCount(open)
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <ConnectForm
          key={count}
          onDone={() => onOpenChange(false)}
          {...props}
        />
      </DialogContent>
    </Dialog>
  )
}

function ConnectForm({
  scope,
  parts,
  source: from,
  onConnected,
  onDone,
}: {
  scope: KnowledgeScope
  parts: Parts
  source: MapEnd | undefined
  onConnected: (connection: Connection) => void
  onDone: () => void
}) {
  const id = React.useId()
  // The form shows why it failed.
  const create = connectionsResource
    .scope(scope)
    .useCreate({ meta: { silent: true } })
  const [source, setSource] = React.useState(from ? endValue(from) : "")
  const [target, setTarget] = React.useState("")
  const [kind, setKind] = React.useState<ConnectionKind>()
  const [description, setDescription] = React.useState("")
  const [submitted, setSubmitted] = React.useState(false)
  const all = [...parts.values()]
  const items = all.map((part) => ({ value: endValue(part), label: part.name }))
  const partOf = (value: string) => {
    const end = endFrom(value)
    return end ? parts.get(end.id) : undefined
  }
  // Picked, or what the ends suggest.
  const shownKind = kind ?? suggestedKind(partOf(source), partOf(target))

  function submit(event: React.SubmitEvent<HTMLFormElement>) {
    event.preventDefault()
    setSubmitted(true)
    const start = endFrom(source)
    const end = endFrom(target)
    if (!start || !end) return
    create.mutate(
      {
        ...endFields("source", start),
        ...endFields("target", end),
        kind: shownKind,
        description: description.trim(),
      },
      {
        onSuccess: (link) => {
          toast.add({
            title: `Connected ${parts.get(start.id)?.name} to ${parts.get(end.id)?.name}`,
            type: "success",
          })
          onConnected(link)
          onDone()
        },
      }
    )
  }

  const endSelect = (
    field: string,
    value: string,
    exclude: string,
    onChange: (value: string) => void
  ) => {
    const options = all.filter((part) => endValue(part) !== exclude)
    const applications = options.filter((part) => part.type === "application")
    const components = options.filter((part) => part.type === "component")
    const group = (label: string, list: Part[]) =>
      list.length > 0 && (
        <SelectGroup>
          <SelectLabel>{label}</SelectLabel>
          {list.map((part) => (
            <SelectItem key={part.id} value={endValue(part)}>
              <PartLead part={part} />
              {part.name}
            </SelectItem>
          ))}
        </SelectGroup>
      )
    return (
      <Select
        items={items}
        value={value || null}
        onValueChange={(next) => onChange(next ? String(next) : "")}
      >
        <SelectTrigger
          id={`${id}-${field}`}
          className="w-full"
          aria-invalid={submitted && !value}
        >
          <SelectValue placeholder="Pick an application or component" />
        </SelectTrigger>
        <SelectContent alignItemWithTrigger={false}>
          {group("Applications", applications)}
          {group("Components", components)}
        </SelectContent>
      </Select>
    )
  }

  return (
    <form onSubmit={submit} noValidate className="grid gap-6">
      <DialogHeader>
        <DialogTitle>Connect</DialogTitle>
        <DialogDescription>
          Draw how two parts of the system work together: from the one that
          calls, writes, publishes or depends on, to the other. Agents searching
          the knowledge base are told the connections.
        </DialogDescription>
      </DialogHeader>
      {create.error && (
        <ErrorCallout title="Couldn't connect them">
          {create.error.message}
        </ErrorCallout>
      )}
      <FieldGroup>
        <Field data-invalid={submitted && !source}>
          <FieldLabel htmlFor={`${id}-source`}>From</FieldLabel>
          {endSelect("source", source, "", (next) => {
            setSource(next)
            if (next === target) setTarget("")
          })}
          {submitted && !source && (
            <FieldError>Pick an application or component.</FieldError>
          )}
        </Field>
        <Field>
          <FieldLabel htmlFor={`${id}-kind`}>Connection</FieldLabel>
          <Select
            items={CONNECTION_KIND_ITEMS}
            value={shownKind}
            onValueChange={(next) => next && setKind(next as ConnectionKind)}
          >
            <SelectTrigger id={`${id}-kind`} className="w-full">
              <SelectValue />
            </SelectTrigger>
            <SelectContent alignItemWithTrigger={false}>
              {CONNECTION_KIND_ITEMS.map((item) => (
                <SelectItem key={item.value} value={item.value}>
                  {item.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <FieldDescription>
            {CONNECTION_KINDS[shownKind].description}.
          </FieldDescription>
        </Field>
        <Field data-invalid={submitted && !target}>
          <FieldLabel htmlFor={`${id}-target`}>To</FieldLabel>
          {endSelect("target", target, source, setTarget)}
          {submitted && !target && (
            <FieldError>Pick what it connects to.</FieldError>
          )}
        </Field>
        <Field>
          <FieldLabel htmlFor={`${id}-note`}>Note</FieldLabel>
          <Textarea
            id={`${id}-note`}
            value={description}
            maxLength={1000}
            rows={2}
            placeholder="Optional, e.g. POST /v1/orders, the orders.v1 topic, or the invoices table"
            onChange={(event) => setDescription(event.target.value)}
          />
        </Field>
      </FieldGroup>
      <DialogFooter>
        <DialogClose render={<Button variant="outline" />}>Cancel</DialogClose>
        <Button type="submit" disabled={create.isPending}>
          {create.isPending && <Spinner data-icon="inline-start" />}
          Connect
        </Button>
      </DialogFooter>
    </form>
  )
}

/**
 * Adds a component (of the kind picked in Add, which can change) or
 * changes one: what it is, its name, what it runs on and its description,
 * which agents read; a new one may go straight into a group.
 */
export function ComponentDialog({
  open,
  onOpenChange,
  ...props
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  scope: KnowledgeScope
  baseName: string
  editing: Extract<Editing, { type: "component" }> | undefined
  groups: Group[]
  onSaved: (component: Component) => void
}) {
  const count = useOpenCount(open)
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        {props.editing && (
          <ComponentForm
            key={count}
            {...props}
            editing={props.editing}
            onDone={() => onOpenChange(false)}
          />
        )}
      </DialogContent>
    </Dialog>
  )
}

function ComponentForm({
  scope,
  baseName,
  editing,
  groups,
  onSaved,
  onDone,
}: {
  scope: KnowledgeScope
  baseName: string
  editing: Extract<Editing, { type: "component" }>
  groups: Group[]
  onSaved: (component: Component) => void
  onDone: () => void
}) {
  const id = React.useId()
  const existing = editing.component
  const refresh = useDesignRefresh(scope)
  const scoped = componentsResource.scope(scope)
  // The form shows why it failed.
  const create = scoped.useCreate({ meta: { silent: true } })
  const update = scoped.useUpdate({ meta: { silent: true } })
  const saving = existing ? update : create
  const [kind, setKind] = React.useState<ComponentKind>(
    existing?.kind ?? editing.kind
  )
  const [name, setName] = React.useState(existing?.name ?? "")
  const [technology, setTechnology] = React.useState(existing?.technology ?? "")
  const [description, setDescription] = React.useState(
    existing?.description ?? ""
  )
  const [group, setGroup] = React.useState(editing.groupId)
  const [submitted, setSubmitted] = React.useState(false)
  const about = COMPONENT_KINDS[kind]

  function submit(event: React.SubmitEvent<HTMLFormElement>) {
    event.preventDefault()
    setSubmitted(true)
    if (!name.trim()) return
    const body = {
      kind,
      name: name.trim(),
      technology: technology.trim(),
      description: description.trim(),
    }
    const done = (component: Component) => {
      onSaved(component)
      onDone()
    }
    if (existing) {
      update.mutate({ id: existing.id, data: body }, { onSuccess: done })
    } else {
      create.mutate(
        { ...body, ...(group && { group_id: group }) },
        {
          onSuccess: (component) => {
            toast.add({ title: `Added ${component.name}`, type: "success" })
            // It's in its group on the map: read the map again.
            if (group) void refresh()
            done(component)
          },
        }
      )
    }
  }

  return (
    <form onSubmit={submit} noValidate className="grid gap-6">
      <DialogHeader>
        <DialogTitle>
          {existing ? `Edit ${existing.name}` : "Add a component"}
        </DialogTitle>
        <DialogDescription>
          A part of the system that isn't one of {baseName}'s applications'
          code. Agents searching the knowledge base are told what it is and how
          it connects.
        </DialogDescription>
      </DialogHeader>
      {saving.error && (
        <ErrorCallout title={existing ? "Couldn't save it" : "Couldn't add it"}>
          {saving.error.message}
        </ErrorCallout>
      )}
      <FieldGroup>
        <Field>
          <FieldLabel htmlFor={`${id}-kind`}>Kind</FieldLabel>
          <Select
            items={COMPONENT_KIND_ITEMS}
            value={kind}
            onValueChange={(next) => next && setKind(next as ComponentKind)}
          >
            <SelectTrigger id={`${id}-kind`} className="w-full">
              <SelectValue />
            </SelectTrigger>
            <SelectContent alignItemWithTrigger={false}>
              {COMPONENT_KIND_ITEMS.map((item) => (
                <SelectItem key={item.value} value={item.value}>
                  <Icon icon={COMPONENT_KINDS[item.value].icon} />
                  {item.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <FieldDescription>{about.description}.</FieldDescription>
        </Field>
        <Field data-invalid={submitted && !name.trim()}>
          <FieldLabel htmlFor={`${id}-name`}>Name</FieldLabel>
          <Input
            id={`${id}-name`}
            value={name}
            maxLength={200}
            autoFocus
            aria-invalid={submitted && !name.trim()}
            placeholder={
              kind === "external"
                ? "e.g. Stripe"
                : `e.g. orders-${kind === "queue" ? "events" : kind}`
            }
            onChange={(event) => setName(event.target.value)}
          />
          {submitted && !name.trim() && (
            <FieldError>Give it a name.</FieldError>
          )}
        </Field>
        <Field>
          <FieldLabel htmlFor={`${id}-technology`}>Runs on</FieldLabel>
          <Input
            id={`${id}-technology`}
            value={technology}
            maxLength={200}
            placeholder={`Optional, e.g. ${about.technology}`}
            onChange={(event) => setTechnology(event.target.value)}
          />
        </Field>
        <Field>
          <FieldLabel htmlFor={`${id}-description`}>Description</FieldLabel>
          <Textarea
            id={`${id}-description`}
            value={description}
            maxLength={4000}
            rows={3}
            placeholder="What it holds or does, and anything agents should know: who owns it, what's sensitive"
            onChange={(event) => setDescription(event.target.value)}
          />
        </Field>
        {!existing && groups.length > 0 && (
          <Field>
            <FieldLabel htmlFor={`${id}-group`}>Group</FieldLabel>
            <GroupPicker
              id={`${id}-group`}
              value={group}
              groups={groups}
              onChange={(next) => setGroup(next ?? undefined)}
            />
          </Field>
        )}
      </FieldGroup>
      <DialogFooter>
        <DialogClose render={<Button variant="outline" />}>Cancel</DialogClose>
        <Button type="submit" disabled={saving.isPending}>
          {saving.isPending && <Spinner data-icon="inline-start" />}
          {existing ? "Save" : "Add component"}
        </Button>
      </DialogFooter>
    </form>
  )
}

/** Adds a group, or changes one: its name and what it is and is for. */
export function GroupDialog({
  open,
  onOpenChange,
  ...props
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  scope: KnowledgeScope
  baseName: string
  /** The group being changed; none to add one. */
  group: Group | undefined
  onSaved: (group: Group) => void
}) {
  const count = useOpenCount(open)
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <GroupForm key={count} {...props} onDone={() => onOpenChange(false)} />
      </DialogContent>
    </Dialog>
  )
}

function GroupForm({
  scope,
  baseName,
  group: existing,
  onSaved,
  onDone,
}: {
  scope: KnowledgeScope
  baseName: string
  group: Group | undefined
  onSaved: (group: Group) => void
  onDone: () => void
}) {
  const id = React.useId()
  const scoped = groupsResource.scope(scope)
  // The form shows why it failed.
  const create = scoped.useCreate({ meta: { silent: true } })
  const update = scoped.useUpdate({ meta: { silent: true } })
  const saving = existing ? update : create
  const [name, setName] = React.useState(existing?.name ?? "")
  const [description, setDescription] = React.useState(
    existing?.description ?? ""
  )
  const [submitted, setSubmitted] = React.useState(false)

  function submit(event: React.SubmitEvent<HTMLFormElement>) {
    event.preventDefault()
    setSubmitted(true)
    if (!name.trim()) return
    const body = { name: name.trim(), description: description.trim() }
    const done = (group: Group) => {
      onSaved(group)
      onDone()
    }
    if (existing) {
      update.mutate({ id: existing.id, data: body }, { onSuccess: done })
    } else {
      create.mutate(body, { onSuccess: done })
    }
  }

  return (
    <form onSubmit={submit} noValidate className="grid gap-6">
      <DialogHeader>
        <DialogTitle>
          {existing ? `Edit ${existing.name}` : "Add a group"}
        </DialogTitle>
        <DialogDescription>
          A part of the system drawn around some of its applications and
          components: a domain, a bounded context, a team's services, a network.
          Agents searching {baseName} read what it's for and what's in it.
        </DialogDescription>
      </DialogHeader>
      {saving.error && (
        <ErrorCallout title={existing ? "Couldn't save it" : "Couldn't add it"}>
          {saving.error.message}
        </ErrorCallout>
      )}
      <FieldGroup>
        <Field data-invalid={submitted && !name.trim()}>
          <FieldLabel htmlFor={`${id}-name`}>Name</FieldLabel>
          <Input
            id={`${id}-name`}
            value={name}
            maxLength={200}
            autoFocus
            aria-invalid={submitted && !name.trim()}
            placeholder="e.g. Payments"
            onChange={(event) => setName(event.target.value)}
          />
          {submitted && !name.trim() && (
            <FieldError>Give it a name.</FieldError>
          )}
        </Field>
        <Field>
          <FieldLabel htmlFor={`${id}-description`}>
            What it is and what it's for
          </FieldLabel>
          <Textarea
            id={`${id}-description`}
            value={description}
            maxLength={4000}
            rows={4}
            placeholder="e.g. Charges customers and refunds them. Owned by the billing team; the ledger is the source of truth for money."
            onChange={(event) => setDescription(event.target.value)}
          />
          <FieldDescription>
            Put applications and components in it from the inspector.
          </FieldDescription>
        </Field>
      </FieldGroup>
      <DialogFooter>
        <DialogClose render={<Button variant="outline" />}>Cancel</DialogClose>
        <Button type="submit" disabled={saving.isPending}>
          {saving.isPending && <Spinner data-icon="inline-start" />}
          {existing ? "Save" : "Add group"}
        </Button>
      </DialogFooter>
    </form>
  )
}
