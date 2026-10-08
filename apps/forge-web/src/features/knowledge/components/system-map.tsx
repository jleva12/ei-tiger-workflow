import * as React from "react"
import { cn } from "cn"

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
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Skeleton } from "@/components/ui/skeleton"
import { Spinner } from "@/components/ui/spinner"
import { Textarea } from "@/components/ui/textarea"
import { toast } from "@/components/ui/toast"
import { DeleteDialog } from "@/features/admin/components/delete-dialog"
import { useOpenCount } from "@/features/admin/components/dialog-state"
import type { CodeRepository } from "@/features/code-repositories/lib/api"
import { fullName } from "@/features/code-repositories/lib/display"
import { parseTimestamp } from "@/lib/timestamps"
import { useActorLabel } from "@/lib/users"
import type { KnowledgeScope } from "../lib/api"
import {
  CONNECTION_KIND_ITEMS,
  CONNECTION_KINDS,
  connections as connectionsResource,
  useSaveSystemMapLayout,
  useSystemMapLayout,
  type Connection,
  type ConnectionKind,
  type SystemMapUpdate,
} from "../lib/system"
import { CodeLinksSection } from "./code-links"
import type { MapSelection } from "./system-map-canvas"

// Cytoscape is large; load it with the map, not with the app.
const SystemMapCanvas = React.lazy(() =>
  import("./system-map-canvas").then((module) => ({
    default: module.SystemMapCanvas,
  }))
)

// The applications' markers, by their place in the knowledge base's list.
const COLORS = [
  "bg-project-1",
  "bg-project-2",
  "bg-project-3",
  "bg-project-4",
] as const

// The map and the inspector side by side; narrower, the map on top.
const LAYOUT =
  "grid min-h-[30rem] min-w-0 flex-1 grid-cols-[minmax(0,1fr)_clamp(18rem,28%,24rem)] grid-rows-[minmax(0,1fr)] gap-3 @max-[1050px]/shell:min-h-0 @max-[1050px]/shell:flex-none @max-[1050px]/shell:grid-cols-1 @max-[1050px]/shell:grid-rows-[26rem_auto]"

const PANEL = "min-h-0 min-w-0 rounded-(--radius-card) border bg-card"

// Keys in a column as wide as the longest; values take the rest.
const FACTS = "grid grid-cols-[fit-content(40%)_minmax(0,1fr)] gap-x-4 gap-y-2"

const addedFormat = new Intl.DateTimeFormat(undefined, {
  dateStyle: "medium",
  timeStyle: "short",
})

const plural = (count: number, word: string) =>
  `${count} ${word}${count === 1 ? "" : "s"}`

type Placed = { repo: CodeRepository; color: number }

/**
 * A system design knowledge base's map: each of its applications (code
 * repositories) as a node, and the connections between them (one calling
 * another's API, depending on it, sending it events or sharing its data)
 * as arrows. Ingestion doesn't find connections across applications, so
 * those who manage knowledge bases draw them here: select an application
 * and connect it to another, or use Connect applications. Everyone in the
 * organization sees the map; an application opens its code graph.
 */
export function SystemMap({
  scope,
  baseName,
  applications,
  canManage,
  onOpen,
}: {
  scope: KnowledgeScope
  baseName: string
  /** The knowledge base's applications, in its list's order. */
  applications: CodeRepository[]
  /** `knowledge_bases:manage` in the organization. */
  canManage: boolean
  /** Opens an application's code graph, at a node when one's given. */
  onOpen: (repositoryId: string, nodeId?: string) => void
}) {
  const scoped = connectionsResource.scope(scope)
  const list = scoped.useList()
  // Where its applications are, the same for everyone; those who manage
  // knowledge bases save what they move.
  const layout = useSystemMapLayout(scope)
  const { mutate: saveLayoutMutate } = useSaveSystemMapLayout(scope)
  const saveLayout = React.useCallback(
    (update: SystemMapUpdate) => saveLayoutMutate(update),
    [saveLayoutMutate]
  )
  // The dialog shows why removing failed.
  const remove = scoped.useDelete({ meta: { silent: true } })
  const [selection, setSelection] = React.useState<MapSelection>(null)
  const [connecting, setConnecting] = React.useState(false)
  const [connectFrom, setConnectFrom] = React.useState<string>()
  const [removing, setRemoving] = React.useState<Connection>()

  const placed = React.useMemo(
    () =>
      new Map<string, Placed>(
        applications.map((repo, index) => [repo.id, { repo, color: index }])
      ),
    [applications]
  )
  // A connection whose application has just left goes with it.
  const shown = React.useMemo(
    () =>
      (list.data ?? []).filter(
        (link) =>
          placed.has(link.source_repository_id) &&
          placed.has(link.target_repository_id)
      ),
    [list.data, placed]
  )
  const selectedApplication =
    selection?.type === "application" ? placed.get(selection.id) : undefined
  const selectedConnection =
    selection?.type === "connection"
      ? shown.find((link) => link.id === selection.id)
      : undefined
  // A selection that's gone (removed, or left the knowledge base) is none.
  const current = selectedApplication || selectedConnection ? selection : null

  const openApplication = React.useCallback(
    (id: string) => onOpen(id),
    [onOpen]
  )
  function connect(from?: string) {
    setConnectFrom(from)
    setConnecting(true)
  }

  const removingEnds = removing && {
    source:
      placed.get(removing.source_repository_id)?.repo.name ?? "the application",
    target: placed.get(removing.target_repository_id)?.repo.name ?? "the other",
  }

  return (
    <div className="flex h-full min-w-0 flex-col gap-3 p-4 @max-[1050px]/shell:h-auto">
      <div className="flex min-h-7 items-center gap-3">
        <p className="min-w-0 truncate text-xs text-muted-foreground tabular-nums">
          {plural(applications.length, "application")}
          {list.data && ` · ${plural(shown.length, "connection")}`}
        </p>
        {canManage && (
          <Button
            size="sm"
            variant="outline"
            className="ml-auto"
            disabled={applications.length < 2}
            onClick={() => connect(selectedApplication?.repo.id)}
          >
            <Icon icon="link" data-icon="inline-start" />
            Connect applications
          </Button>
        )}
      </div>
      {list.error && !list.data && (
        <ErrorCallout
          title="Couldn't load the connections"
          action={
            <Button
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
      )}
      <div className={LAYOUT}>
        <div className={`${PANEL} overflow-hidden`}>
          {!layout.data ? (
            layout.error ? (
              <div className="flex size-full items-center justify-center p-6">
                <ErrorCallout
                  title="Couldn't load the map's layout"
                  action={
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() => void layout.refetch()}
                    >
                      Retry
                    </Button>
                  }
                >
                  {layout.error.message}
                </ErrorCallout>
              </div>
            ) : (
              <Skeleton className="size-full rounded-none" />
            )
          ) : (
            <React.Suspense
              fallback={<Skeleton className="size-full rounded-none" />}
            >
              <SystemMapCanvas
                saved={layout.data}
                onSave={canManage ? saveLayout : undefined}
                applications={applications}
                connections={shown}
                selection={current}
                onSelect={setSelection}
                onOpen={openApplication}
              />
            </React.Suspense>
          )}
        </div>
        <aside
          className={`${PANEL} flex flex-col overflow-hidden`}
          aria-label="Inspector"
        >
          <header className="flex h-10 shrink-0 items-center justify-between border-b pr-1.5 pl-3">
            <h3 className="text-xs font-medium text-foreground">
              {selectedApplication
                ? "Application"
                : selectedConnection
                  ? "Connection"
                  : "Applications"}
            </h3>
            {current && (
              <Button
                variant="ghost"
                size="icon-xs"
                aria-label="Back to all applications"
                onClick={() => setSelection(null)}
              >
                <Icon icon="close" />
              </Button>
            )}
          </header>
          <div
            tabIndex={0}
            className="min-h-0 flex-1 overflow-y-auto p-3 @max-[1050px]/shell:max-h-96"
          >
            {selectedApplication ? (
              <ApplicationDetails
                key={selectedApplication.repo.id}
                baseName={baseName}
                application={selectedApplication}
                connections={shown}
                placed={placed}
                canManage={canManage}
                alone={applications.length < 2}
                onConnect={() => connect(selectedApplication.repo.id)}
                onOpen={openApplication}
                onSelect={setSelection}
                onRemove={setRemoving}
              />
            ) : selectedConnection ? (
              <ConnectionDetails
                scope={scope}
                connection={selectedConnection}
                placed={placed}
                canManage={canManage}
                onSelect={setSelection}
                onRemove={setRemoving}
                onOpenNode={onOpen}
              />
            ) : (
              <ApplicationList
                applications={applications}
                connections={shown}
                canManage={canManage}
                onSelect={setSelection}
              />
            )}
          </div>
        </aside>
      </div>
      {canManage && (
        <ConnectDialog
          open={connecting}
          onOpenChange={setConnecting}
          scope={scope}
          applications={applications}
          source={connectFrom}
          onConnected={(link) =>
            setSelection({ type: "connection", id: link.id })
          }
        />
      )}
      <DeleteDialog
        open={removing !== undefined}
        onClose={() => setRemoving(undefined)}
        title={`Remove the connection from ${removingEnds?.source} to ${removingEnds?.target}?`}
        description="It's taken off the system map, with its code links; nothing changes in either repository."
        confirmLabel="Remove connection"
        onConfirm={() => remove.mutateAsync(removing!.id)}
      />
    </div>
  )
}

/** An application's colour, as its marker in the list. */
function Marker({ color }: { color: number }) {
  return (
    <span
      aria-hidden="true"
      className={cn(
        "size-2 shrink-0 rounded-[3px]",
        COLORS[color % COLORS.length]
      )}
    />
  )
}

function RowButton({
  onClick,
  lead,
  title,
  detail,
}: {
  onClick: () => void
  lead: React.ReactNode
  title: string
  detail: string
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="flex w-full min-w-0 flex-1 items-center gap-2 rounded-(--radius-item) px-1.5 py-1 text-left text-xs hover:bg-muted"
    >
      {lead}
      <span className="flex min-w-0 flex-col">
        <span className="truncate text-foreground">{title}</span>
        <span className="truncate text-2xs text-subtle">{detail}</span>
      </span>
    </button>
  )
}

/** Nothing selected: every application, with how many connections it has. */
function ApplicationList({
  applications,
  connections,
  canManage,
  onSelect,
}: {
  applications: CodeRepository[]
  connections: Connection[]
  canManage: boolean
  onSelect: (selection: MapSelection) => void
}) {
  const pending = applications.some((repo) => !repo.last_success)
  return (
    <div className="flex min-w-0 flex-col gap-3 text-xs">
      <p className="text-muted-foreground">
        {canManage
          ? "Select an application to see how it connects to the others, or to connect it: what it calls, what it sends messages to, what it depends on."
          : "Select an application to see how it connects to the others."}
      </p>
      <ul className="-mx-1 flex flex-col" aria-label="Applications">
        {applications.map((repo, index) => {
          const count = connections.filter(
            (link) =>
              link.source_repository_id === repo.id ||
              link.target_repository_id === repo.id
          ).length
          return (
            <li key={repo.id}>
              <RowButton
                onClick={() => onSelect({ type: "application", id: repo.id })}
                lead={<Marker color={index} />}
                title={repo.name}
                detail={count ? plural(count, "connection") : "Not connected"}
              />
            </li>
          )
        })}
      </ul>
      {pending && (
        <p className="flex items-center gap-1.5 text-2xs text-subtle">
          <span
            aria-hidden="true"
            className="h-2.5 w-4 shrink-0 rounded-[3px] border border-dashed border-muted-foreground"
          />
          Dashed: not in the code graph yet
        </p>
      )}
    </div>
  )
}

/** A selected application: what it is, and its connections each way. */
function ApplicationDetails({
  baseName,
  application: { repo, color },
  connections,
  placed,
  canManage,
  alone,
  onConnect,
  onOpen,
  onSelect,
  onRemove,
}: {
  baseName: string
  application: Placed
  connections: Connection[]
  placed: Map<string, Placed>
  canManage: boolean
  /** It's the knowledge base's only application: nothing to connect it to. */
  alone: boolean
  onConnect: () => void
  onOpen: (id: string) => void
  onSelect: (selection: MapSelection) => void
  onRemove: (connection: Connection) => void
}) {
  const outgoing = connections.filter(
    (link) => link.source_repository_id === repo.id
  )
  const incoming = connections.filter(
    (link) => link.target_repository_id === repo.id
  )
  const section = (
    title: string,
    list: Connection[],
    other: (link: Connection) => string
  ) =>
    list.length > 0 && (
      <section className="flex flex-col gap-1.5">
        <h5 className="text-2xs font-medium text-muted-foreground">{title}</h5>
        <ul className="-mx-1 flex flex-col">
          {list.map((link) => {
            const end = placed.get(other(link))
            if (!end) return null
            const kind = [
              CONNECTION_KINDS[link.kind]?.label ?? link.kind,
              link.code_links > 0 && plural(link.code_links, "code link"),
            ]
              .filter(Boolean)
              .join(" · ")
            return (
              <li key={link.id} className="flex items-center gap-1">
                <RowButton
                  onClick={() => onSelect({ type: "connection", id: link.id })}
                  lead={<Marker color={end.color} />}
                  title={end.repo.name}
                  detail={
                    link.description ? `${kind} · ${link.description}` : kind
                  }
                />
                {canManage && (
                  <Button
                    variant="ghost"
                    size="icon-xs"
                    aria-label={`Remove the connection ${title === "Connects to" ? "to" : "from"} ${end.repo.name}`}
                    onClick={() => onRemove(link)}
                  >
                    <Icon icon="close" />
                  </Button>
                )}
              </li>
            )
          })}
        </ul>
      </section>
    )
  return (
    <div className="flex min-w-0 flex-col gap-4 text-xs">
      <div className="flex flex-col gap-1">
        <span className="flex items-center gap-1.5 text-2xs text-muted-foreground">
          <Marker color={color} />
          Application
          {!repo.last_success && " · not in the code graph yet"}
        </span>
        <h4 className="text-sm font-medium break-words text-foreground">
          {repo.name}
        </h4>
        <a
          href={repo.url}
          target="_blank"
          rel="noreferrer"
          className="flex w-fit max-w-full min-w-0 items-center gap-1 font-mono text-2xs text-muted-foreground hover:text-foreground hover:underline"
        >
          <span className="truncate">
            {fullName(repo)} · {repo.branch}
          </span>
          <Icon icon="external" size={12} className="shrink-0" />
        </a>
      </div>
      <div className="flex flex-wrap gap-1.5 *:flex-1">
        {canManage && (
          <Button size="sm" disabled={alone} onClick={onConnect}>
            <Icon icon="link" data-icon="inline-start" />
            Connect to another
          </Button>
        )}
        <Button size="sm" variant="outline" onClick={() => onOpen(repo.id)}>
          <Icon icon="layers" data-icon="inline-start" />
          {repo.last_success ? "Open code graph" : "Open application"}
        </Button>
      </div>
      {section("Connects to", outgoing, (link) => link.target_repository_id)}
      {section("Connected from", incoming, (link) => link.source_repository_id)}
      {outgoing.length + incoming.length === 0 && (
        <p className="text-subtle">
          {alone
            ? `It's ${baseName}'s only application.`
            : canManage
              ? `Not connected to ${baseName}'s other applications yet.`
              : `Not connected to ${baseName}'s other applications yet. Those who manage knowledge bases can connect it.`}
        </p>
      )}
    </div>
  )
}

/**
 * A selected connection: its two applications, how, where it happens in
 * their code, and who added it.
 */
function ConnectionDetails({
  scope,
  connection,
  placed,
  canManage,
  onSelect,
  onRemove,
  onOpenNode,
}: {
  scope: KnowledgeScope
  connection: Connection
  placed: Map<string, Placed>
  canManage: boolean
  onSelect: (selection: MapSelection) => void
  onRemove: (connection: Connection) => void
  onOpenNode: (repositoryId: string, nodeId: string) => void
}) {
  const actor = useActorLabel()
  const kind = CONNECTION_KINDS[connection.kind]
  const source = placed.get(connection.source_repository_id)
  const target = placed.get(connection.target_repository_id)
  const end = (id: string) => {
    const found = placed.get(id)
    return (
      found && (
        <Button
          variant="link"
          size="xs"
          className="h-auto gap-1.5 p-0"
          onClick={() => onSelect({ type: "application", id })}
        >
          <Marker color={found.color} />
          {found.repo.name}
        </Button>
      )
    )
  }
  return (
    <div className="flex min-w-0 flex-col gap-4 text-xs">
      <div className="flex flex-col gap-1">
        <span className="text-2xs text-muted-foreground">
          Connection · drawn by hand
        </span>
        <h4 className="text-sm font-medium text-foreground">
          {kind?.label ?? connection.kind}
        </h4>
        {kind && <p className="text-muted-foreground">{kind.description}</p>}
      </div>
      <div className="flex flex-col items-start gap-0.5">
        {end(connection.source_repository_id)}
        <span className="text-2xs text-subtle">
          ↓ {kind?.label ?? connection.kind}
        </span>
        {end(connection.target_repository_id)}
      </div>
      {connection.description && (
        <p className="rounded-(--radius-item) border bg-muted px-2.5 py-2 break-words whitespace-pre-wrap text-foreground">
          {connection.description}
        </p>
      )}
      {source && target && (
        <CodeLinksSection
          key={connection.id}
          scope={scope}
          connection={connection}
          ends={{ source: source.repo, target: target.repo }}
          canManage={canManage}
          onOpenNode={onOpenNode}
        />
      )}
      <dl className={FACTS}>
        <dt className="leading-5 text-muted-foreground">Added by</dt>
        <dd className="min-w-0 leading-5 break-words text-foreground">
          {actor(connection.created_by)}
        </dd>
        <dt className="leading-5 text-muted-foreground">Added</dt>
        <dd className="min-w-0 leading-5 text-foreground">
          {addedFormat.format(parseTimestamp(connection.created_at))}
        </dd>
      </dl>
      {canManage && (
        <Button
          size="sm"
          variant="destructive"
          className="self-start"
          onClick={() => onRemove(connection)}
        >
          Remove connection
        </Button>
      )}
    </div>
  )
}

/**
 * Connects one of the knowledge base's applications to another: from, how
 * and to, read as a sentence ("checkout-web calls orders-api"), with an
 * optional note.
 */
function ConnectDialog({
  open,
  onOpenChange,
  ...props
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  scope: KnowledgeScope
  applications: CodeRepository[]
  /** The application to connect from, when one is selected. */
  source: string | undefined
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
  applications,
  source: from,
  onConnected,
  onDone,
}: {
  scope: KnowledgeScope
  applications: CodeRepository[]
  source: string | undefined
  onConnected: (connection: Connection) => void
  onDone: () => void
}) {
  const id = React.useId()
  // The form shows why it failed.
  const create = connectionsResource
    .scope(scope)
    .useCreate({ meta: { silent: true } })
  const [source, setSource] = React.useState(from ?? "")
  const [target, setTarget] = React.useState("")
  const [kind, setKind] = React.useState<ConnectionKind>("calls")
  const [description, setDescription] = React.useState("")
  const [submitted, setSubmitted] = React.useState(false)
  const items = applications.map((repo) => ({
    value: repo.id,
    label: repo.name,
  }))
  const targets = items.filter((item) => item.value !== source)
  const name = (repoId: string) =>
    applications.find((repo) => repo.id === repoId)?.name ?? "the application"

  function submit(event: React.SubmitEvent<HTMLFormElement>) {
    event.preventDefault()
    setSubmitted(true)
    if (!source || !target) return
    create.mutate(
      {
        source_repository_id: source,
        target_repository_id: target,
        kind,
        description: description.trim(),
      },
      {
        onSuccess: (link) => {
          toast.add({
            title: `Connected ${name(link.source_repository_id)} to ${name(link.target_repository_id)}`,
            type: "success",
          })
          onConnected(link)
          onDone()
        },
      }
    )
  }

  const applicationSelect = (
    field: string,
    value: string,
    options: typeof items,
    onChange: (value: string) => void
  ) => (
    <Select
      items={options}
      value={value || null}
      onValueChange={(next) => onChange(next ?? "")}
    >
      <SelectTrigger
        id={`${id}-${field}`}
        className="w-full"
        aria-invalid={submitted && !value}
      >
        <SelectValue placeholder="Pick an application" />
      </SelectTrigger>
      <SelectContent alignItemWithTrigger={false}>
        {options.map((item) => (
          <SelectItem key={item.value} value={item.value}>
            {item.label}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  )

  return (
    <form onSubmit={submit} noValidate className="grid gap-6">
      <DialogHeader>
        <DialogTitle>Connect applications</DialogTitle>
        <DialogDescription>
          Draw how two applications work together: from the one that calls,
          depends on, sends messages to or shares data with the other. Agents
          searching the knowledge base are told the connections.
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
          {applicationSelect("source", source, items, (next) => {
            setSource(next)
            if (next === target) setTarget("")
          })}
          {submitted && !source && (
            <FieldError>Pick an application.</FieldError>
          )}
        </Field>
        <Field>
          <FieldLabel htmlFor={`${id}-kind`}>Connection</FieldLabel>
          <Select
            items={CONNECTION_KIND_ITEMS}
            value={kind}
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
            {CONNECTION_KINDS[kind].description}.
          </FieldDescription>
        </Field>
        <Field data-invalid={submitted && !target}>
          <FieldLabel htmlFor={`${id}-target`}>To</FieldLabel>
          {applicationSelect("target", target, targets, setTarget)}
          {submitted && !target && (
            <FieldError>Pick the application it connects to.</FieldError>
          )}
        </Field>
        <Field>
          <FieldLabel htmlFor={`${id}-note`}>Note</FieldLabel>
          <Textarea
            id={`${id}-note`}
            value={description}
            maxLength={1000}
            rows={2}
            placeholder="Optional, e.g. POST /v1/orders, or the orders topic"
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
