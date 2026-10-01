import * as React from "react"
import { cn } from "cn"

import { DeleteDialog } from "@/components/admin/delete-dialog"
import { PageEmpty } from "@/components/forge/empty-state"
import { ErrorCallout } from "@/components/forge/feedback"
import { Icon } from "@/components/forge/icon"
import { ShellHeaderActions, useShellPage } from "@/components/forge/shell"
import { Chip, CountBadge } from "@/components/forge/status"
import {
  TaskMeta,
  TaskPage,
  TaskPageHeader,
  TaskTabBar,
  TaskTabsList,
  TaskTabsTrigger,
  TaskTitleRow,
} from "@/components/forge/task-page"
import { Button } from "@/components/ui/button"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import {
  Field,
  FieldDescription,
  FieldError,
  FieldGroup,
  FieldLabel,
} from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { Skeleton } from "@/components/ui/skeleton"
import { Spinner } from "@/components/ui/spinner"
import { Tabs, TabsContent } from "@/components/ui/tabs"
import { Textarea } from "@/components/ui/textarea"
import { toast } from "@/components/ui/toast"
import type { MyOrganization } from "@/lib/access"
import { toApiError, type ApiError } from "@/lib/api"
import {
  EVENT_KEY_PATTERN,
  EVENT_TYPE_STATUS,
  EVENTS_ICON,
  eventTypes,
  isEventTypeTab,
  useEventEndpoint,
  useSchemaTrial,
  type EventType,
  type EventTypeTab,
} from "@/lib/events"
import { useScopeAccess } from "@/lib/hierarchy"
import {
  buildJsonSchema,
  checkDraft,
  emptyDraft,
  exampleFrom,
  keyFrom,
  parseJsonSchema,
  type SchemaDraft,
} from "@/lib/json-schema"
import { useSaveWorkflows, useOrganizationWorkflows } from "@/lib/workflows/api"
import { eventKeysOf, renameEvent, unlinkEvent } from "@/lib/workflows/triggers"
import { EventTriggers } from "./event-triggers"
import { JsonSchemaBuilder } from "./json-schema-builder"
import { IssueList, ReceivedEvents } from "./received-events"
import { SendingGuide } from "./sending-guide"

/** The detail pages fill the page body and scroll themselves. */
function FullBleed({ children }: { children: React.ReactNode }) {
  return <div className="absolute inset-0 flex flex-col">{children}</div>
}

// A tab's body, on the same gutters as the header and tabs above it, so
// the details line up under the title at any width.
const BODY =
  "w-full px-[30px] pt-7 pb-[60px] @max-[900px]/shell:px-5 @max-[600px]/shell:px-[15px]"

// Two columns divided by a hairline, stacked on narrow shells: the first a
// third (Definition) or a half (Test).
const SPLIT = {
  third:
    "grid grid-cols-[minmax(17rem,1fr)_minmax(0,2fr)] @max-[1050px]/shell:grid-cols-1",
  half: "grid grid-cols-2 @max-[1050px]/shell:grid-cols-1",
}
const LEAD_COLUMN =
  "min-w-0 pr-10 @max-[1050px]/shell:pr-0 @max-[1050px]/shell:pb-8"
const MAIN_COLUMN =
  "min-w-0 border-l pl-10 @max-[1050px]/shell:border-t @max-[1050px]/shell:border-l-0 @max-[1050px]/shell:pt-8 @max-[1050px]/shell:pl-0"

/**
 * One of an organization's event types, or a new one (`eventTypeId` "new"), under
 * Events in the top bar's trail. Definition puts its details (name, key,
 * description) in the left third and the schema its payload must match,
 * built property by property, in the rest; Test checks a sample payload
 * against the schema being edited, beside how to send one; Received is
 * what it has received; Triggers links it to the organization's workflows it
 * starts. Its actions are in the top bar: a new type is
 * created as a draft, which accepts nothing until it's turned on after a
 * review. Those who manage the organization's events edit it; everyone else in the
 * organization reads it.
 */
export function EventTypePage({
  organization,
  eventTypeId,
  tab,
  onTabChange,
  onBack,
  onCreated,
}: {
  organization: MyOrganization
  eventTypeId: string
  tab: EventTypeTab
  onTabChange: (tab: EventTypeTab) => void
  onBack: () => void
  onCreated: (id: string) => void
}) {
  const creating = eventTypeId === "new"
  const scoped = eventTypes.scope({ organizationId: organization.id })
  const detail = scoped.useDetail(creating ? undefined : eventTypeId)
  const can = useScopeAccess(`org:${organization.id}`)

  // The trail ends in Events, then this type.
  useShellPage({
    header: {
      title: creating ? "New event type" : (detail.data?.name ?? "Event type"),
      icon: EVENTS_ICON,
      breadcrumbs: [
        { label: organization.name, href: `/organizations/${organization.id}` },
        {
          label: "Events",
          href: `/organizations/${organization.id}?view=events`,
        },
      ],
    },
  })

  if (!creating && !detail.data) {
    if (detail.error) {
      return detail.error.status === 404 ? (
        <PageEmpty
          illustration="search"
          title="Event type not found"
          description={`It isn't one of ${organization.name}'s event types anymore.`}
        >
          <Button variant="outline" size="sm" onClick={onBack}>
            All event types
          </Button>
        </PageEmpty>
      ) : (
        <ErrorCallout
          title="Couldn't load the event type"
          action={
            <Button
              variant="outline"
              size="sm"
              onClick={() => void detail.refetch()}
            >
              Retry
            </Button>
          }
        >
          {detail.error.message}
        </ErrorCallout>
      )
    }
    return (
      <FullBleed>
        <TaskPage aria-busy="true">
          <TaskPageHeader>
            <Skeleton className="mb-4 h-8 w-72" />
            <Skeleton className="h-4 w-96 max-w-full" />
          </TaskPageHeader>
          <div className={cn(BODY, SPLIT.third)}>
            <div className={cn(LEAD_COLUMN, "flex flex-col gap-4")}>
              <Skeleton className="h-8 w-full" />
              <Skeleton className="h-8 w-full" />
              <Skeleton className="h-24 w-full" />
            </div>
            <div className={cn(MAIN_COLUMN, "flex flex-col gap-2")}>
              <Skeleton className="h-10 w-full" />
              <Skeleton className="h-10 w-full" />
              <Skeleton className="h-10 w-full" />
            </div>
          </div>
        </TaskPage>
      </FullBleed>
    )
  }

  return (
    <EventTypeEditor
      // A fresh form for each event type.
      key={eventTypeId}
      organizationId={organization.id}
      organizationName={organization.name}
      saved={creating ? undefined : detail.data}
      canManage={can("events:manage")}
      tab={
        creating && (tab === "received" || tab === "triggers")
          ? "definition"
          : tab
      }
      onTabChange={onTabChange}
      onBack={onBack}
      onCreated={onCreated}
    />
  )
}

type Form = {
  name: string
  key: string
  description: string
}

function EventTypeEditor({
  organizationId,
  organizationName,
  saved,
  canManage,
  tab,
  onTabChange,
  onBack,
  onCreated,
}: {
  organizationId: string
  organizationName: string
  /** The event type as saved; none while creating one. */
  saved: EventType | undefined
  canManage: boolean
  tab: EventTypeTab
  onTabChange: (tab: EventTypeTab) => void
  onBack: () => void
  onCreated: (id: string) => void
}) {
  const id = React.useId()
  const scoped = eventTypes.scope({ organizationId })
  // The page shows why saving failed; turning on or pausing toasts.
  const create = scoped.useCreate({ meta: { silent: true } })
  const update = scoped.useUpdate({ meta: { silent: true } })
  const turn = scoped.useUpdate({
    meta: { errorTitle: "Couldn't change the event type" },
  })
  const remove = scoped.useDelete({ meta: { silent: true } })
  const endpoint = useEventEndpoint(organizationId)
  // The workflows it starts: they name it by key.
  const { records } = useOrganizationWorkflows(organizationId)
  const saveWorkflows = useSaveWorkflows(organizationId)
  const linked = saved
    ? records.filter((r) => eventKeysOf(r.document).includes(saved.key))
    : []

  // What the saved schema reads as, and what the builder can't show of it.
  const [initial] = React.useState(() =>
    saved
      ? parseJsonSchema(saved.payload_schema)
      : { draft: emptyDraft(), warnings: [] as string[] }
  )
  // Saving from here drops what the builder can't show.
  const [lossy, setLossy] = React.useState(initial.warnings)
  const [form, setForm] = React.useState<Form>({
    name: saved?.name ?? "",
    key: saved?.key ?? "",
    description: saved?.description ?? "",
  })
  // A new type's key follows its name until it's typed.
  const [keyTyped, setKeyTyped] = React.useState(Boolean(saved))
  const [draft, setDraft] = React.useState<SchemaDraft>(initial.draft)
  const [submitted, setSubmitted] = React.useState(false)
  const [error, setError] = React.useState<ApiError>()
  const [deleting, setDeleting] = React.useState(false)

  const schema = React.useMemo(() => buildJsonSchema(draft), [draft])
  const issues = React.useMemo(() => checkDraft(draft), [draft])
  const [baseline, setBaseline] = React.useState(() =>
    JSON.stringify({ ...form, schema: buildJsonSchema(initial.draft) })
  )
  const dirty = JSON.stringify({ ...form, schema }) !== baseline
  const pending = create.isPending || update.isPending
  const status = saved?.status ?? "draft"

  const nameError = submitted && !form.name.trim() ? "Name it." : undefined
  const keyError =
    error?.status === 409
      ? error.message
      : submitted && !EVENT_KEY_PATTERN.test(form.key)
        ? "Lowercase letters, digits and . _ -, starting with a letter."
        : undefined

  const change = (patch: Partial<Form>) => {
    setForm((current) => ({ ...current, ...patch }))
    if (error) setError(undefined)
  }

  async function save() {
    setSubmitted(true)
    if (
      !form.name.trim() ||
      !EVENT_KEY_PATTERN.test(form.key) ||
      issues.size > 0
    ) {
      onTabChange("definition")
      return
    }
    setError(undefined)
    const body = {
      name: form.name.trim(),
      key: form.key,
      description: form.description.trim(),
      payload_schema: schema,
    }
    try {
      if (!saved) {
        const made = await create.mutateAsync(body)
        toast.add({
          title: `${made.name} saved as a draft.`,
          description:
            "Review its schema and try a payload, then turn it on to accept events.",
          type: "success",
        })
        onCreated(made.id)
        return
      }
      const updated = await update.mutateAsync({ id: saved.id, data: body })
      setBaseline(JSON.stringify({ ...form, schema }))
      setLossy([])
      // A new key keeps its links: the workflows follow it.
      const relinked =
        updated.key !== saved.key && linked.length > 0
          ? await saveWorkflows(
              linked.map((record) => ({
                record,
                document: renameEvent(record.document, saved.key, updated.key),
              }))
            )
          : undefined
      toast.add({
        title: "Saved.",
        description:
          updated.schema_version !== saved.schema_version
            ? `Events from now on are checked against schema version ${updated.schema_version}.`
            : undefined,
        type: "success",
      })
      if (relinked && !relinked.ok) {
        toast.add({
          title: "Couldn't move its links to the new key",
          description: `Linked workflows still start on ${saved.key}: ${relinked.error}`,
          type: "error",
        })
      }
    } catch (caught) {
      setError(toApiError(caught))
    }
  }

  function setStatus(next: "active" | "paused") {
    if (!saved) return
    turn.mutate(
      { id: saved.id, data: { status: next } },
      {
        onSuccess: (updated) =>
          toast.add({
            title:
              next === "active"
                ? `${updated.name} is accepting events.`
                : `${updated.name} is paused.`,
            description:
              next === "active"
                ? `Senders can post ${updated.key} now.`
                : "Senders get 409 until you turn it on again.",
            type: "success",
          }),
      }
    )
  }

  const title = saved?.name ?? (form.name.trim() || "New event type")
  const { label: statusLabel, tone: statusTone } = EVENT_TYPE_STATUS[status]
  const keyPreview = `${endpoint.data?.url ?? "…/hooks/events/<endpoint>"}/${form.key || "<key>"}`

  const actions = canManage && (
    <ShellHeaderActions>
      {saved ? (
        <>
          <DropdownMenu>
            <DropdownMenuTrigger
              render={
                <Button
                  variant="ghost"
                  size="icon"
                  aria-label={`More actions for ${saved.name}`}
                />
              }
            >
              <Icon icon="more" />
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              <DropdownMenuItem
                variant="destructive"
                onClick={() => setDeleting(true)}
              >
                Delete event type…
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
          <Button
            variant={dirty ? "default" : "outline"}
            disabled={!dirty || pending}
            onClick={() => void save()}
          >
            {pending && <Spinner data-icon="inline-start" />}
            Save
          </Button>
          {status === "active" ? (
            <Button
              variant="outline"
              disabled={turn.isPending}
              onClick={() => setStatus("paused")}
            >
              {turn.isPending && <Spinner data-icon="inline-start" />}
              Pause
            </Button>
          ) : (
            // Turning on takes the saved definition: save changes first.
            <Button
              variant={dirty ? "outline" : "default"}
              disabled={dirty || turn.isPending}
              onClick={() => setStatus("active")}
            >
              {turn.isPending && <Spinner data-icon="inline-start" />}
              Turn on
            </Button>
          )}
        </>
      ) : (
        <>
          <Button variant="ghost" onClick={onBack}>
            Cancel
          </Button>
          <Button disabled={pending} onClick={() => void save()}>
            {pending && <Spinner data-icon="inline-start" />}
            Create draft
          </Button>
        </>
      )}
    </ShellHeaderActions>
  )

  return (
    <FullBleed>
      {actions}
      <TaskPage>
        <TaskPageHeader>
          <TaskTitleRow
            title={title}
            meta={
              <>
                <Chip tone={statusTone}>{statusLabel}</Chip>
                {saved ? (
                  <>
                    <code className="font-mono text-xs text-foreground">
                      {saved.key}
                    </code>
                    <TaskMeta icon="code">
                      Schema v{saved.schema_version}
                    </TaskMeta>
                    <TaskMeta icon="activity">
                      {saved.event_count.toLocaleString()} received
                      {saved.invalid_count > 0 &&
                        ` · ${saved.invalid_count.toLocaleString()} invalid`}
                    </TaskMeta>
                  </>
                ) : (
                  <span>
                    Saved as a draft: it accepts nothing until you turn it on.
                  </span>
                )}
                {saved && dirty && <Chip tone="warning">Unsaved changes</Chip>}
              </>
            }
          >
            {saved && status !== "active" && (
              <p
                className={cn(
                  "mt-4 flex max-w-[78ch] items-start gap-2 rounded-(--radius-card) border px-3 py-2 text-[0.8125rem]",
                  status === "draft"
                    ? "border-notice-border bg-notice-surface text-notice-foreground"
                    : "text-muted-foreground"
                )}
              >
                <Icon
                  icon={status === "draft" ? "clock" : "info"}
                  size={15}
                  className="mt-0.5 shrink-0"
                />
                {status === "draft"
                  ? canManage
                    ? `A draft refuses events, so senders get 409. Review the schema, try a payload on Test, then turn it on.`
                    : `A draft refuses events until one of ${organizationName}'s admins turns it on.`
                  : "Paused: senders get 409 until it's turned on again. Events already received are kept."}
              </p>
            )}
          </TaskTitleRow>
        </TaskPageHeader>

        <Tabs
          value={tab}
          onValueChange={(value) => {
            if (isEventTypeTab(value)) onTabChange(value)
          }}
          className="gap-0"
        >
          <TaskTabBar>
            <TaskTabsList aria-label="Event type pages">
              <TaskTabsTrigger value="definition" icon="settings">
                Definition
              </TaskTabsTrigger>
              <TaskTabsTrigger value="test" icon="sparkles">
                Test
              </TaskTabsTrigger>
              {saved && (
                <>
                  <TaskTabsTrigger value="received" icon="activity">
                    Received
                  </TaskTabsTrigger>
                  <TaskTabsTrigger value="triggers" icon="link">
                    Triggers
                    {linked.length > 0 && (
                      <CountBadge>{linked.length}</CountBadge>
                    )}
                  </TaskTabsTrigger>
                </>
              )}
            </TaskTabsList>
          </TaskTabBar>

          <TabsContent value="definition">
            <div className={cn(BODY, SPLIT.third)}>
              <section
                aria-labelledby={`${id}-details`}
                className={cn(
                  LEAD_COLUMN,
                  // Stays in view beside a long schema.
                  "flex flex-col gap-5 self-start @max-[1050px]/shell:max-w-[640px] @min-[1051px]/shell:sticky @min-[1051px]/shell:top-7"
                )}
              >
                <div className="flex flex-col gap-1">
                  <h2 id={`${id}-details`} className="text-sm font-medium">
                    Details
                  </h2>
                  <p className="text-xs text-muted-foreground">
                    What {organizationName} calls it, and the key senders post
                    it with.
                  </p>
                </div>
                {error && error.status !== 409 && (
                  <ErrorCallout
                    title={`Couldn't ${saved ? "save" : "create"} the event type`}
                  >
                    {error.message}
                  </ErrorCallout>
                )}
                <FieldGroup className="gap-5">
                  <Field data-invalid={Boolean(nameError)}>
                    <FieldLabel htmlFor={`${id}-name`}>Name</FieldLabel>
                    <Input
                      id={`${id}-name`}
                      value={form.name}
                      placeholder="Incident opened"
                      maxLength={200}
                      autoComplete="off"
                      autoFocus={!saved}
                      disabled={!canManage}
                      aria-invalid={Boolean(nameError)}
                      onChange={(event) =>
                        change({
                          name: event.target.value,
                          ...(!keyTyped && {
                            key: keyFrom(event.target.value),
                          }),
                        })
                      }
                    />
                    {nameError && <FieldError>{nameError}</FieldError>}
                  </Field>
                  <Field data-invalid={Boolean(keyError)}>
                    <FieldLabel htmlFor={`${id}-key`}>Key</FieldLabel>
                    <Input
                      id={`${id}-key`}
                      value={form.key}
                      placeholder="incident.opened"
                      maxLength={100}
                      autoComplete="off"
                      spellCheck={false}
                      disabled={!canManage}
                      aria-invalid={Boolean(keyError)}
                      onChange={(event) => {
                        setKeyTyped(true)
                        change({ key: event.target.value.trim() })
                      }}
                      className="font-mono"
                    />
                    {keyError ? (
                      <FieldError>{keyError}</FieldError>
                    ) : (
                      <FieldDescription className="flex flex-col gap-1">
                        <span>
                          {saved && form.key !== saved.key
                            ? "Senders post to a new URL once it's saved:"
                            : "The end of the URL senders post it to:"}
                        </span>
                        <span className="font-mono text-2xs break-all text-foreground/80">
                          {keyPreview}
                        </span>
                      </FieldDescription>
                    )}
                  </Field>
                  <Field>
                    <FieldLabel htmlFor={`${id}-description`}>
                      Description
                    </FieldLabel>
                    <Textarea
                      id={`${id}-description`}
                      rows={5}
                      value={form.description}
                      placeholder="Sent by PagerDuty when an incident opens on one of our services."
                      disabled={!canManage}
                      onChange={(event) =>
                        change({ description: event.target.value })
                      }
                    />
                    <FieldDescription>
                      Optional. Who sends it and when.
                    </FieldDescription>
                  </Field>
                </FieldGroup>
              </section>

              <div className={cn(MAIN_COLUMN, "flex flex-col gap-5")}>
                {submitted && issues.size > 0 && (
                  <ErrorCallout title="Fix the schema first">
                    {issues.size === 1
                      ? "One property needs attention; it's marked below."
                      : `${issues.size} properties need attention; they're marked below.`}
                  </ErrorCallout>
                )}
                {lossy.length > 0 && (
                  <div className="rounded-(--radius-card) bg-warning-surface px-3 py-2 text-xs text-warning-foreground">
                    <p className="mb-1 font-medium">
                      The saved schema has parts this builder can't show. Saving
                      from here leaves them out:
                    </p>
                    <ul className="list-disc pl-4">
                      {lossy.map((warning) => (
                        <li key={warning}>{warning}</li>
                      ))}
                    </ul>
                  </div>
                )}
                <JsonSchemaBuilder
                  value={draft}
                  onChange={setDraft}
                  readOnly={!canManage}
                  description="What every event of this type must look like. One that doesn't match is kept, marked invalid, and answered 422 so the sender knows."
                />
              </div>
            </div>
          </TabsContent>

          <TabsContent value="test">
            <div className={cn(BODY, SPLIT.half)}>
              <SchemaTrial
                organizationId={organizationId}
                draft={draft}
                schema={schema}
                edited={dirty}
                className={LEAD_COLUMN}
              />
              <SendingGuide
                url={endpoint.data?.url}
                eventKey={form.key || "incident.opened"}
                className={MAIN_COLUMN}
              />
            </div>
          </TabsContent>

          {saved && (
            <TabsContent value="received">
              <div className={BODY}>
                <ReceivedEvents
                  organizationId={organizationId}
                  eventTypeId={saved.id}
                />
              </div>
            </TabsContent>
          )}

          {saved && (
            <TabsContent value="triggers">
              <EventTriggers
                organizationId={organizationId}
                eventType={saved}
                className={BODY}
              />
            </TabsContent>
          )}
        </Tabs>
      </TaskPage>

      {saved && (
        <DeleteDialog
          open={deleting}
          onClose={() => setDeleting(false)}
          title={`Delete ${saved.name}?`}
          description={[
            saved.event_count > 0
              ? `Its ${saved.event_count.toLocaleString()} received events are deleted with it. Senders still posting ${saved.key} get 404.`
              : `Senders still posting ${saved.key} get 404.`,
            linked.length > 0
              ? `${linked.length === 1 ? "The workflow it starts is" : `The ${linked.length} workflows it starts are`} unlinked from it.`
              : "",
          ]
            .filter(Boolean)
            .join(" ")}
          confirmLabel="Delete event type"
          onConfirm={async () => {
            await remove.mutateAsync(saved.id)
            // Its workflows keep starting on their other event types.
            if (linked.length > 0) {
              await saveWorkflows(
                linked.map((record) => ({
                  record,
                  document: unlinkEvent(record.document, saved.key),
                }))
              )
            }
            toast.add({ title: `${saved.name} deleted.`, type: "success" })
            onBack()
          }}
        />
      )}
    </FullBleed>
  )
}

/** A sample payload, checked against the schema as it's being edited. */
function SchemaTrial({
  organizationId,
  draft,
  schema,
  edited,
  className,
}: {
  organizationId: string
  draft: SchemaDraft
  schema: Record<string, unknown>
  /** The schema has changes not saved yet. */
  edited: boolean
  className?: string
}) {
  const id = React.useId()
  const trial = useSchemaTrial(organizationId)
  const [text, setText] = React.useState(() =>
    JSON.stringify(exampleFrom(draft), null, 2)
  )
  const [parseError, setParseError] = React.useState<string>()

  function check() {
    let payload: unknown
    try {
      payload = JSON.parse(text)
    } catch (caught) {
      setParseError(`Not JSON: ${(caught as Error).message}`)
      trial.reset()
      return
    }
    setParseError(undefined)
    trial.mutate({ payload_schema: schema, payload })
  }

  return (
    <section
      aria-labelledby={`${id}-title`}
      className={cn("flex flex-col gap-4", className)}
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex flex-col gap-1">
          <h2 id={`${id}-title`} className="text-sm font-medium">
            Try a payload
          </h2>
          <p className="max-w-[62ch] text-[0.8125rem] text-muted-foreground">
            Checked the way the endpoint will check it, against the schema on
            Definition{edited ? " with its unsaved changes" : ""}. Nothing is
            stored.
          </p>
        </div>
        <Button
          variant="ghost"
          size="sm"
          onClick={() => {
            setText(JSON.stringify(exampleFrom(draft), null, 2))
            trial.reset()
          }}
        >
          <Icon icon="refresh" data-icon="inline-start" />
          Example from schema
        </Button>
      </div>
      <label htmlFor={`${id}-payload`} className="sr-only">
        Payload
      </label>
      <Textarea
        id={`${id}-payload`}
        rows={14}
        spellCheck={false}
        value={text}
        aria-invalid={Boolean(parseError)}
        onChange={(event) => setText(event.target.value)}
        className="min-h-64 font-mono text-xs md:text-xs"
      />
      {parseError && <FieldError>{parseError}</FieldError>}
      <div className="flex items-center gap-3">
        <Button
          variant="outline"
          onClick={check}
          disabled={trial.isPending || !text.trim()}
        >
          {trial.isPending && <Spinner data-icon="inline-start" />}
          Check payload
        </Button>
        {trial.data?.valid && (
          <Chip tone="success" icon="check">
            Matches the schema
          </Chip>
        )}
      </div>
      {trial.error && (
        <ErrorCallout title="Couldn't check it">
          {trial.error.message}
        </ErrorCallout>
      )}
      {trial.data && !trial.data.valid && (
        <IssueList issues={trial.data.errors} />
      )}
    </section>
  )
}
