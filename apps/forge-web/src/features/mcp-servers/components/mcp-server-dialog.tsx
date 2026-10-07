import * as React from "react"
import { Delete02Icon } from "@hugeicons/core-free-icons"

import { ErrorCallout } from "@/components/forge/feedback"
import { Icon } from "@/components/forge/icon"
import { Chip } from "@/components/forge/status"
import { Button } from "@/components/ui/button"
import {
  Field,
  FieldDescription,
  FieldError,
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
import { Skeleton } from "@/components/ui/skeleton"
import { Spinner } from "@/components/ui/spinner"
import { Textarea } from "@/components/ui/textarea"
import { toApiError, type ApiError } from "@/lib/api/index"
import { formatRelative } from "@/lib/format"
import { useOpenCount } from "@/features/admin/components/dialog-state"
import {
  SettingsBody,
  SettingsDialog,
  SettingsFooter,
  SettingsGlyph,
  SettingsHeader,
  SettingsHint,
  SettingsSection,
} from "@/features/builder/components/settings-dialog"
import {
  useAuthMethods,
  useMcpServerDefaults,
  type AuthMethodInfo,
  type FieldHelp,
  type McpServerCreate,
  type McpServerDefault,
  type McpServerRecord,
} from "@/features/mcp-servers/lib/api"
import {
  MCP_SERVERS_ICON,
  plural,
  STATUS_DISPLAY,
  statusOf,
} from "@/features/mcp-servers/lib/display"

type McpServerDialogProps = {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** The server to show and edit; without one the dialog adds a server. */
  server?: McpServerRecord
  /** Show it without letting it be changed (no `mcp_servers:manage`). */
  readOnly?: boolean
  /** Adds (without `server`); rejects with the API's error, which it shows. */
  onCreate?: (input: McpServerCreate) => Promise<unknown>
  /** Saves `server`; rejects with the API's error, which it shows. */
  onUpdate?: (id: string, input: McpServerCreate) => Promise<unknown>
  /** Connects to it and lists its tools. */
  onCheck?: (server: McpServerRecord) => void
  checking?: boolean
  /** Sends the browser to sign in to its authorization server. */
  onConnect?: (server: McpServerRecord) => Promise<unknown>
  onDisconnect?: (server: McpServerRecord) => void
  onDelete?: (server: McpServerRecord) => void
}

/**
 * Adds an MCP server, or shows and edits one, in the same dialog as a
 * workflow's or an agent's step settings: where it is, how Forge
 * authenticates to it (any auth method the admin API offers, its form
 * drawn from the method's fields), and what it had when last checked.
 * Unlike a step, it's saved with Save: its secrets go to the API once.
 */
export function McpServerDialog({
  open,
  onOpenChange,
  server,
  ...props
}: McpServerDialogProps) {
  const count = useOpenCount(open)
  // While it closes it keeps showing what it showed: a server, or (null) a
  // new one.
  const [shown, setShown] = React.useState<McpServerRecord | null | undefined>(
    open ? (server ?? null) : undefined
  )
  const target = open ? (server ?? null) : undefined
  if (target !== undefined && target !== shown) setShown(target)
  return (
    <SettingsDialog open={open} onOpenChange={onOpenChange}>
      {shown ? (
        <McpServerForm
          // A new form for each opening, and once a new server is added.
          key={`${count}:${shown.id}`}
          server={shown}
          onDone={() => onOpenChange(false)}
          {...props}
        />
      ) : (
        shown === null && (
          <NewMcpServer
            key={count}
            onDone={() => onOpenChange(false)}
            {...props}
          />
        )
      )}
    </SettingsDialog>
  )
}

type FormProps = Omit<McpServerDialogProps, "open" | "onOpenChange" | "server">

/**
 * A server being added: first a choice between the servers the application
 * offers ready-made and a new, empty one, then the form, filled in with the
 * one chosen. Without any to offer, the form straight away.
 */
function NewMcpServer({
  onDone,
  ...props
}: FormProps & { onDone: () => void }) {
  const defaults = useMcpServerDefaults()
  // undefined while choosing; null for a new, empty server.
  const [start, setStart] = React.useState<McpServerDefault | null>()
  const offered = defaults.data ?? []
  const choosing =
    start === undefined && !(defaults.isSuccess && offered.length === 0)
  if (choosing)
    return (
      <ServerChooser
        defaults={defaults}
        onChoose={(chosen) => setStart(chosen)}
        onDone={onDone}
      />
    )
  return (
    <McpServerForm
      key={start?.id ?? "new"}
      template={start ?? undefined}
      onBack={offered.length > 0 ? () => setStart(undefined) : undefined}
      onDone={onDone}
      {...props}
    />
  )
}

/** The servers the application offers ready-made, and a new, empty one. */
function ServerChooser({
  defaults,
  onChoose,
  onDone,
}: {
  defaults: ReturnType<typeof useMcpServerDefaults>
  onChoose: (chosen: McpServerDefault | null) => void
  onDone: () => void
}) {
  const methods = useAuthMethods()
  const methodLabel = (kind: string) =>
    methods.data?.find((m) => m.kind === kind)?.label ?? kind
  return (
    <>
      <SettingsHeader
        glyph={<SettingsGlyph icon={MCP_SERVERS_ICON} tone="action" />}
        title="Add an MCP server"
        description="Start from a server Forge knows how to connect to, or from scratch"
        closeLabel="Close adding an MCP server"
      />
      <SettingsBody>
        <SettingsSection title="Application MCP servers">
          <p className="text-xs/[1.6] text-muted-foreground">
            Ready-made: where each is and how Forge signs in to it are filled
            in, so you only add what&apos;s yours.
          </p>
          {defaults.isPending ? (
            <div className="flex flex-col gap-2" aria-busy="true">
              <Skeleton className="h-16" />
              <Skeleton className="h-16" />
            </div>
          ) : defaults.error ? (
            <ErrorCallout
              title="Couldn't load the application's MCP servers"
              action={
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  onClick={() => void defaults.refetch()}
                >
                  Retry
                </Button>
              }
            >
              {defaults.error.message}
            </ErrorCallout>
          ) : (
            <ul className="flex flex-col divide-y rounded-(--radius-item) border">
              {defaults.data.map((server) => (
                <li key={server.id}>
                  <ChoiceButton
                    icon={MCP_SERVERS_ICON}
                    title={server.name}
                    chip={methodLabel(server.auth.kind)}
                    description={server.description}
                    detail={server.url}
                    onClick={() => onChoose(server)}
                  />
                </li>
              ))}
            </ul>
          )}
        </SettingsSection>
        <SettingsSection title="Another server">
          <div className="rounded-(--radius-item) border">
            <ChoiceButton
              icon="plus"
              title="New MCP server"
              description="Any remote MCP server over streamable HTTP: you fill in where it is and how Forge signs in to it."
              onClick={() => onChoose(null)}
            />
          </div>
        </SettingsSection>
      </SettingsBody>
      <SettingsFooter>
        <SettingsHint>Pick one to set it up</SettingsHint>
        <Button
          type="button"
          variant="outline"
          size="sm"
          className="max-[600px]:ml-auto"
          onClick={onDone}
        >
          Cancel
        </Button>
      </SettingsFooter>
    </>
  )
}

/** One choice of the chooser: a row that opens what it names. */
function ChoiceButton({
  icon,
  title,
  chip,
  description,
  detail,
  onClick,
}: {
  icon: React.ComponentProps<typeof SettingsGlyph>["icon"]
  title: string
  chip?: string
  description?: string
  detail?: string
  onClick: () => void
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="group/choice flex w-full items-start gap-3 px-3 py-2.5 text-left transition-[background-color] duration-150 hover:bg-muted/60"
    >
      <SettingsGlyph icon={icon} />
      <span className="flex min-w-0 flex-1 flex-col gap-0.5">
        <span className="flex min-w-0 items-center gap-2">
          <span className="truncate text-[0.8125rem] font-medium text-foreground">
            {title}
          </span>
          {chip && <Chip>{chip}</Chip>}
        </span>
        {description && (
          <span className="line-clamp-2 text-2xs/[1.6] text-muted-foreground">
            {description}
          </span>
        )}
        {detail && (
          <span className="truncate font-mono text-2xs text-subtle">
            {detail}
          </span>
        )}
      </span>
      <Icon
        icon="right"
        className="mt-1.5 shrink-0 text-subtle transition-colors group-hover/choice:text-foreground"
      />
    </button>
  )
}

type HeaderDraft = {
  id: string
  name: string
  value: string
  /** From a default server that needs it: kept, and filled in. */
  required?: boolean
  description?: string
}

const draftId = () => Math.random().toString(36).slice(2, 10)

const URL_PATTERN = /^https?:\/\/[^\s/?#]+\S*$/
const HEADER_NAME = /^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$/

function defaultsOf(method: AuthMethodInfo | undefined) {
  return Object.fromEntries(
    (method?.fields ?? [])
      .filter((f) => !f.secret)
      .map((f) => [f.name, f.default])
  )
}

function McpServerForm({
  server,
  template,
  onBack,
  readOnly = false,
  onCreate,
  onUpdate,
  onCheck,
  checking = false,
  onConnect,
  onDisconnect,
  onDelete,
  onDone,
}: Omit<McpServerDialogProps, "open" | "onOpenChange"> & {
  /** A new server's starting point: one the application offers. */
  template?: McpServerDefault
  /** Back to choosing what to start from. */
  onBack?: () => void
  onDone: () => void
}) {
  const id = React.useId()
  const methods = useAuthMethods()
  // A server shows what it has; a new one starts from its template, if any.
  const start = server ?? template
  const [name, setName] = React.useState(start?.name ?? "")
  const [description, setDescription] = React.useState(start?.description ?? "")
  const [url, setUrl] = React.useState(start?.url ?? "")
  const [timeout, setTimeoutText] = React.useState(
    String(start?.timeout_seconds ?? 30)
  )
  const [headers, setHeaders] = React.useState<HeaderDraft[]>(() =>
    server
      ? server.headers.map((h) => ({ ...h, id: draftId() }))
      : (template?.headers.map((h) => ({
          id: draftId(),
          name: h.name,
          value: h.value,
          required: h.required,
          description: h.description,
        })) ?? [])
  )
  const [kind, setKind] = React.useState(start?.auth.kind ?? "none")
  const [settings, setSettings] = React.useState<Record<string, string>>(
    start?.auth.settings ?? {}
  )
  // Secrets typed in; those left empty keep what's saved.
  const [secrets, setSecrets] = React.useState<Record<string, string>>({})
  // Saved secrets to remove.
  const [cleared, setCleared] = React.useState<string[]>([])
  const [dirty, setDirty] = React.useState(false)
  const [submitted, setSubmitted] = React.useState(false)
  const [pending, setPending] = React.useState(false)
  const [error, setError] = React.useState<ApiError>()

  const method = methods.data?.find((m) => m.kind === kind)
  // The template's help with its method's fields, while that's the method.
  const help =
    template && template.auth.kind === kind ? template.auth.fields : {}
  // A server's saved secrets count only while its method stays the same.
  const saved =
    server && server.auth.kind === kind ? server.auth.secrets_set : []
  // The method's defaults under what's set (a new server's, before they load).
  const values = { ...defaultsOf(method), ...settings }
  const touch = () => setDirty(true)

  // A new method starts from its defaults.
  const pickKind = (next: string) => {
    setKind(next)
    setSecrets({})
    setCleared([])
    const defaults = defaultsOf(methods.data?.find((m) => m.kind === next))
    setSettings(
      start && start.auth.kind === next
        ? { ...defaults, ...start.auth.settings }
        : defaults
    )
    touch()
  }

  const seconds = Number(timeout)
  const problems = {
    name: !name.trim() ? "Give the server a name." : undefined,
    url: !URL_PATTERN.test(url.trim())
      ? "A URL starts with https:// (or http://)."
      : undefined,
    timeout:
      !Number.isFinite(seconds) || seconds <= 0 || seconds > 300
        ? "Between 1 and 300 seconds."
        : undefined,
    headers: headers.some((h) => !HEADER_NAME.test(h.name.trim()))
      ? "Every header needs a name of letters, digits and - (no spaces)."
      : headers
          .filter((h) => h.required && !h.value.trim())
          .map((h) => `The server needs ${h.name}: give it a value.`)[0],
    fields: Object.fromEntries(
      (method?.fields ?? [])
        .filter((f) => f.required)
        .filter((f) =>
          f.secret
            ? !secrets[f.name]?.trim() &&
              (!saved.includes(f.name) || cleared.includes(f.name))
            : !values[f.name]?.trim()
        )
        .map((f) => [f.name, `${help[f.name]?.label ?? f.label} is needed.`])
    ) as Record<string, string>,
  }
  const invalid =
    problems.name ||
    problems.url ||
    problems.timeout ||
    problems.headers ||
    Object.keys(problems.fields).length > 0
  const shown = (message: string | undefined) =>
    submitted ? message : undefined

  async function submit(event: React.SubmitEvent<HTMLFormElement>) {
    event.preventDefault()
    setSubmitted(true)
    if (invalid || !method) return
    setPending(true)
    setError(undefined)
    const input: McpServerCreate = {
      name: name.trim(),
      description: description.trim(),
      url: url.trim(),
      headers: headers.map((h) => ({ name: h.name.trim(), value: h.value })),
      timeout_seconds: seconds,
      auth: {
        kind,
        settings: Object.fromEntries(
          method.fields
            .filter((f) => !f.secret)
            .map((f) => [f.name, (values[f.name] ?? "").trim()])
        ),
        secrets: {
          ...Object.fromEntries(cleared.map((name) => [name, null])),
          ...Object.fromEntries(
            Object.entries(secrets).filter(([, value]) => value.trim())
          ),
        },
      },
    }
    try {
      if (server) await onUpdate?.(server.id, input)
      else await onCreate?.(input)
      // Saved: the form starts over from what's saved (its key changes for a new one).
      setSecrets({})
      setCleared([])
      setDirty(false)
      setSubmitted(false)
    } catch (caught) {
      setError(toApiError(caught))
    } finally {
      setPending(false)
    }
  }

  const status = server ? STATUS_DISPLAY[statusOf(server)] : undefined
  return (
    <>
      <SettingsHeader
        glyph={<SettingsGlyph icon={MCP_SERVERS_ICON} tone="action" />}
        title={
          server
            ? server.name
            : template
              ? `Add ${template.name}`
              : "New MCP server"
        }
        description={
          server
            ? `MCP server · ${method?.label ?? server.auth.kind} · ${status?.label}`
            : template
              ? "Application MCP server · Streamable HTTP"
              : "MCP server · Streamable HTTP"
        }
        closeLabel="Close the MCP server's settings"
      />
      <form
        onSubmit={submit}
        noValidate
        className="flex min-h-0 flex-1 flex-col"
      >
        <SettingsBody>
          <SettingsSection className="gap-3">
            <p className="text-xs/[1.6] text-muted-foreground">
              A remote MCP server, over streamable HTTP. Its tools can be
              attached to the organization&apos;s agents.
            </p>
            {template?.instructions && (
              <div
                role="note"
                aria-label="What it needs"
                className="flex gap-2.5 rounded-(--radius-item) border border-notice-border bg-notice-surface p-3 text-xs/[1.6] text-foreground"
              >
                <Icon
                  icon="info"
                  className="mt-0.5 shrink-0 text-notice-accent"
                />
                <span>{template.instructions}</span>
              </div>
            )}
            {error && (
              <ErrorCallout
                title={`Couldn't ${server ? "save" : "add"} the MCP server`}
              >
                {error.message}
              </ErrorCallout>
            )}
            {server && (
              <ServerConnection
                server={server}
                readOnly={readOnly}
                dirty={dirty}
                checking={checking}
                onCheck={onCheck}
                onConnect={onConnect}
                onDisconnect={onDisconnect}
              />
            )}
          </SettingsSection>

          <SettingsSection>
            <Field data-invalid={Boolean(shown(problems.name))}>
              <FieldLabel htmlFor={`${id}-name`}>Name</FieldLabel>
              <Input
                id={`${id}-name`}
                autoFocus={!server && !readOnly}
                maxLength={200}
                autoComplete="off"
                disabled={readOnly}
                value={name}
                placeholder="Help center"
                aria-invalid={Boolean(shown(problems.name))}
                onChange={(event) => {
                  setName(event.target.value)
                  touch()
                }}
              />
              {shown(problems.name) && <FieldError>{problems.name}</FieldError>}
            </Field>
            <Field data-invalid={Boolean(shown(problems.url))}>
              <FieldLabel htmlFor={`${id}-url`}>Server URL</FieldLabel>
              <Input
                id={`${id}-url`}
                className="font-mono text-xs"
                maxLength={2048}
                autoComplete="off"
                spellCheck={false}
                disabled={readOnly}
                value={url}
                placeholder="https://mcp.example.com/mcp"
                aria-invalid={Boolean(shown(problems.url))}
                onChange={(event) => {
                  setUrl(event.target.value)
                  touch()
                }}
              />
              {shown(problems.url) ? (
                <FieldError>{problems.url}</FieldError>
              ) : (
                <FieldDescription>
                  Its streamable HTTP endpoint.
                  {server &&
                    " A new URL disconnects it until it's checked or signed in to again."}
                </FieldDescription>
              )}
            </Field>
            <Field>
              <FieldLabel htmlFor={`${id}-description`}>Description</FieldLabel>
              <Textarea
                id={`${id}-description`}
                rows={2}
                disabled={readOnly}
                value={description}
                placeholder="What its tools are for"
                onChange={(event) => {
                  setDescription(event.target.value)
                  touch()
                }}
              />
            </Field>
          </SettingsSection>

          <SettingsSection title="Authentication">
            <AuthFields
              id={id}
              methods={methods}
              kind={kind}
              method={method}
              onKind={pickKind}
              settings={values}
              help={help}
              onSetting={(name, value) => {
                setSettings((prev) => ({ ...prev, [name]: value }))
                touch()
              }}
              secrets={secrets}
              onSecret={(name, value) => {
                setSecrets((prev) => ({ ...prev, [name]: value }))
                setCleared((prev) => prev.filter((other) => other !== name))
                touch()
              }}
              saved={saved}
              cleared={cleared}
              onClear={(name) => {
                setCleared((prev) => [...prev, name])
                setSecrets((prev) => ({ ...prev, [name]: "" }))
                touch()
              }}
              problems={submitted ? problems.fields : {}}
              readOnly={readOnly}
            />
          </SettingsSection>

          <SettingsSection title="Headers">
            <HeaderRows
              headers={headers}
              readOnly={readOnly}
              error={shown(problems.headers)}
              onChange={(next) => {
                setHeaders(next)
                touch()
              }}
            />
          </SettingsSection>

          <SettingsSection title="Connection">
            <div className="grid grid-cols-2 gap-2.5">
              <Field data-invalid={Boolean(shown(problems.timeout))}>
                <FieldLabel htmlFor={`${id}-timeout`}>
                  Timeout (seconds)
                </FieldLabel>
                <Input
                  id={`${id}-timeout`}
                  type="number"
                  min={1}
                  max={300}
                  disabled={readOnly}
                  value={timeout}
                  aria-invalid={Boolean(shown(problems.timeout))}
                  onChange={(event) => {
                    setTimeoutText(event.target.value)
                    touch()
                  }}
                />
                {shown(problems.timeout) && (
                  <FieldError>{problems.timeout}</FieldError>
                )}
              </Field>
              <Field>
                <FieldLabel htmlFor={`${id}-transport`}>Transport</FieldLabel>
                <Input
                  id={`${id}-transport`}
                  disabled
                  value="Streamable HTTP"
                />
              </Field>
            </div>
          </SettingsSection>

          {server && server.tools.length > 0 && (
            <SettingsSection
              title={`${plural(server.tools.length, "tool")}${statusOf(server) !== "ok" ? " when it last answered" : ""}`}
            >
              <ToolList server={server} />
            </SettingsSection>
          )}
        </SettingsBody>

        <SettingsFooter>
          {!server && onBack && (
            <Button type="button" variant="ghost" size="sm" onClick={onBack}>
              <Icon icon="left" data-icon="inline-start" />
              Back
            </Button>
          )}
          {server && !readOnly && onDelete && (
            <Button
              type="button"
              variant="destructive"
              size="sm"
              onClick={() => onDelete(server)}
            >
              Delete server
            </Button>
          )}
          <SettingsHint>
            {readOnly
              ? "You can't change the organization's MCP servers"
              : dirty
                ? "Unsaved changes"
                : "Secrets are encrypted and never shown again"}
          </SettingsHint>
          {readOnly ? (
            <Button
              type="button"
              size="sm"
              className="max-[600px]:ml-auto"
              onClick={onDone}
            >
              Done
            </Button>
          ) : (
            <>
              <Button
                type="button"
                variant="outline"
                size="sm"
                className="max-[600px]:ml-auto"
                onClick={onDone}
              >
                {server && !dirty ? "Close" : "Cancel"}
              </Button>
              <Button
                type="submit"
                size="sm"
                disabled={pending || (server && !dirty)}
              >
                {pending && <Spinner data-icon="inline-start" />}
                {server ? "Save" : "Add server"}
              </Button>
            </>
          )}
        </SettingsFooter>
      </form>
    </>
  )
}

/* -------------------------------------------------------------------------- */
/* Authentication                                                             */
/* -------------------------------------------------------------------------- */

function AuthFields({
  id,
  methods,
  kind,
  method,
  onKind,
  settings,
  help = {},
  onSetting,
  secrets,
  onSecret,
  saved,
  cleared,
  onClear,
  problems,
  readOnly,
}: {
  id: string
  methods: ReturnType<typeof useAuthMethods>
  kind: string
  method: AuthMethodInfo | undefined
  onKind: (kind: string) => void
  settings: Record<string, string>
  /** How the server being added words some of the method's fields. */
  help?: Record<string, FieldHelp>
  onSetting: (name: string, value: string) => void
  secrets: Record<string, string>
  onSecret: (name: string, value: string) => void
  /** The secrets the server has saved. */
  saved: string[]
  cleared: string[]
  onClear: (name: string) => void
  problems: Record<string, string>
  readOnly: boolean
}) {
  if (methods.isPending)
    return (
      <div className="flex flex-col gap-2" aria-busy="true">
        <Skeleton className="h-8" />
        <Skeleton className="h-8" />
      </div>
    )
  if (methods.error)
    return (
      <ErrorCallout
        title="Couldn't load the ways to authenticate"
        action={
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={() => void methods.refetch()}
          >
            Retry
          </Button>
        }
      >
        {methods.error.message}
      </ErrorCallout>
    )
  const items = (methods.data ?? []).map((m) => ({
    value: m.kind,
    label: m.label,
  }))
  return (
    <>
      <Field>
        <FieldLabel htmlFor={`${id}-auth`}>Method</FieldLabel>
        <Select
          items={items}
          value={kind}
          disabled={readOnly}
          onValueChange={(value) => value && onKind(value)}
        >
          <SelectTrigger id={`${id}-auth`} className="w-full">
            <SelectValue />
          </SelectTrigger>
          <SelectContent alignItemWithTrigger={false}>
            {items.map((item) => (
              <SelectItem key={item.value} value={item.value}>
                {item.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        {method && <FieldDescription>{method.description}</FieldDescription>}
      </Field>
      {method?.fields.map((original) => {
        const own = help[original.name]
        const field = {
          ...original,
          label: own?.label ?? original.label,
          description: own?.description ?? original.description,
          placeholder: own?.placeholder ?? original.placeholder,
        }
        const fieldId = `${id}-auth-${field.name}`
        const problem = problems[field.name]
        const isSaved = field.secret && saved.includes(field.name)
        const isCleared = cleared.includes(field.name)
        return (
          <Field key={`${kind}:${field.name}`} data-invalid={Boolean(problem)}>
            <FieldLabel htmlFor={fieldId}>
              {field.label}
              {/* One with a default is filled in already. */}
              {!field.required && !field.default && (
                <span className="font-normal text-subtle">Optional</span>
              )}
            </FieldLabel>
            <div className="flex gap-2">
              <Input
                id={fieldId}
                type={field.secret ? "password" : "text"}
                className={field.secret ? "font-mono text-xs" : undefined}
                autoComplete={field.secret ? "new-password" : "off"}
                spellCheck={false}
                disabled={readOnly}
                value={
                  field.secret
                    ? (secrets[field.name] ?? "")
                    : (settings[field.name] ?? "")
                }
                placeholder={
                  isSaved && !isCleared
                    ? "Saved · type to replace"
                    : isCleared
                      ? "Removed on save"
                      : field.placeholder
                }
                aria-invalid={Boolean(problem)}
                onChange={(event) =>
                  field.secret
                    ? onSecret(field.name, event.target.value)
                    : onSetting(field.name, event.target.value)
                }
              />
              {isSaved && !isCleared && !field.required && !readOnly && (
                <Button
                  type="button"
                  variant="outline"
                  onClick={() => onClear(field.name)}
                >
                  Remove
                </Button>
              )}
            </div>
            {problem ? (
              <FieldError>{problem}</FieldError>
            ) : (
              field.description && (
                <FieldDescription>{field.description}</FieldDescription>
              )
            )}
          </Field>
        )
      })}
    </>
  )
}

/* -------------------------------------------------------------------------- */
/* Headers                                                                    */
/* -------------------------------------------------------------------------- */

function HeaderRows({
  headers,
  onChange,
  readOnly,
  error,
}: {
  headers: HeaderDraft[]
  onChange: (headers: HeaderDraft[]) => void
  readOnly: boolean
  error?: string
}) {
  const [added, setAdded] = React.useState<string>()
  const change = (header: HeaderDraft, patch: Partial<HeaderDraft>) =>
    onChange(headers.map((h) => (h.id === header.id ? { ...h, ...patch } : h)))
  return (
    <div role="group" aria-label="Headers" className="flex flex-col gap-2">
      <p className="text-xs/[1.6] text-muted-foreground">
        Sent as they are with every request, and shown to everyone in the
        organization: put credentials in Authentication.
      </p>
      {headers.map((header) => (
        <div key={header.id} className="flex flex-col gap-1">
          <div className="grid grid-cols-[minmax(0,2fr)_minmax(0,3fr)_auto] items-center gap-1.5">
            <Input
              aria-label="Header name"
              className="font-mono text-xs"
              value={header.name}
              placeholder="X-Tenant"
              spellCheck={false}
              autoComplete="off"
              // The server needs it by this name.
              disabled={readOnly || header.required}
              autoFocus={header.id === added}
              aria-invalid={
                (Boolean(error) && !HEADER_NAME.test(header.name.trim())) ||
                undefined
              }
              onChange={(event) => change(header, { name: event.target.value })}
            />
            <Input
              aria-label={`${header.name.trim() || "Header"} value`}
              className="font-mono text-xs"
              value={header.value}
              placeholder={header.required ? "Needed" : "Value"}
              spellCheck={false}
              autoComplete="off"
              disabled={readOnly}
              aria-invalid={
                (Boolean(error) && header.required && !header.value.trim()) ||
                undefined
              }
              onChange={(event) =>
                change(header, { value: event.target.value })
              }
            />
            {!readOnly && !header.required && (
              <Button
                type="button"
                variant="ghost"
                size="icon"
                aria-label={`Remove ${header.name.trim() || "header"}`}
                className="text-muted-foreground hover:text-destructive"
                onClick={() =>
                  onChange(headers.filter((h) => h.id !== header.id))
                }
              >
                <Icon icon={Delete02Icon} />
              </Button>
            )}
            {/* Keeps the value as wide as the others'. */}
            {!readOnly && header.required && <span className="size-8" />}
          </div>
          {header.description && (
            <FieldDescription>{header.description}</FieldDescription>
          )}
        </div>
      ))}
      {error && <FieldError>{error}</FieldError>}
      {!readOnly && (
        <Button
          type="button"
          variant="outline"
          size="sm"
          className="border-dashed"
          onClick={() => {
            const header = { id: draftId(), name: "", value: "" }
            setAdded(header.id)
            onChange([...headers, header])
          }}
        >
          <Icon icon="plus" data-icon="inline-start" />
          Add header
        </Button>
      )}
    </div>
  )
}

/* -------------------------------------------------------------------------- */
/* Connection                                                                 */
/* -------------------------------------------------------------------------- */

/** Whether the server answered when last checked, and checking or connecting it. */
function ServerConnection({
  server,
  readOnly,
  dirty,
  checking,
  onCheck,
  onConnect,
  onDisconnect,
}: {
  server: McpServerRecord
  readOnly: boolean
  /** The form has unsaved changes: connect what's saved first. */
  dirty: boolean
  checking: boolean
  onCheck?: (server: McpServerRecord) => void
  onConnect?: (server: McpServerRecord) => Promise<unknown>
  onDisconnect?: (server: McpServerRecord) => void
}) {
  const [connecting, setConnecting] = React.useState(false)
  const [connectError, setConnectError] = React.useState<string>()
  const status = statusOf(server)
  const display = STATUS_DISPLAY[status]
  const { auth } = server
  const info = server.server_info
  const connect = async () => {
    setConnecting(true)
    setConnectError(undefined)
    try {
      // It navigates away when it works.
      await onConnect?.(server)
    } catch (caught) {
      setConnectError(toApiError(caught).message)
      setConnecting(false)
    }
  }

  return (
    <div
      role="group"
      aria-label="Connection"
      className="flex flex-col gap-2.5 rounded-(--radius-item) border bg-muted/40 p-3"
    >
      <div className="flex flex-wrap items-center gap-2">
        <Chip tone={display.tone}>{display.label}</Chip>
        <span className="text-2xs text-muted-foreground">
          {checking
            ? "Checking…"
            : server.checked_at
              ? `Checked ${formatRelative(server.checked_at)}`
              : "Never checked"}
          {info?.name &&
            ` · ${info.name}${info.version ? ` ${info.version}` : ""}`}
        </span>
        {!readOnly && (
          <div className="ml-auto flex gap-1.5">
            {auth.interactive && (
              <Button
                type="button"
                size="sm"
                variant={auth.connected ? "outline" : "default"}
                disabled={connecting || dirty}
                title={dirty ? "Save your changes first" : undefined}
                onClick={() => void connect()}
              >
                {connecting && <Spinner data-icon="inline-start" />}
                {auth.connected ? "Reconnect" : "Sign in"}
              </Button>
            )}
            <Button
              type="button"
              size="sm"
              variant="outline"
              disabled={checking || dirty}
              title={dirty ? "Save your changes first" : undefined}
              onClick={() => onCheck?.(server)}
            >
              {checking ? (
                <Spinner data-icon="inline-start" />
              ) : (
                <Icon icon="refresh" data-icon="inline-start" />
              )}
              Check
            </Button>
          </div>
        )}
      </div>

      {auth.interactive && auth.connected && (
        <p className="flex flex-wrap items-center gap-x-1.5 text-2xs text-muted-foreground">
          <span>
            Signed in
            {auth.connected_by_name && ` by ${auth.connected_by_name}`}
            {auth.connected_at && ` ${formatRelative(auth.connected_at)}`}
            {auth.scope && (
              <>
                {" · "}
                <span className="font-mono">{auth.scope}</span>
              </>
            )}
            . Every agent uses this sign-in.
          </span>
          {!readOnly && (
            <Button
              type="button"
              variant="link"
              size="xs"
              className="h-auto p-0 text-2xs"
              onClick={() => onDisconnect?.(server)}
            >
              Disconnect
            </Button>
          )}
        </p>
      )}

      {connectError && (
        <ErrorCallout title="Couldn't start signing in">
          {connectError}
        </ErrorCallout>
      )}
      {server.last_error && status !== "ok" && !checking && (
        <p className="text-2xs/[1.6] text-destructive">{server.last_error}</p>
      )}
      {status === "needs_auth" && !server.last_error && (
        <p className="text-2xs/[1.6] text-muted-foreground">
          Sign in to the server&apos;s authorization server so agents can use
          it.
        </p>
      )}
    </div>
  )
}

/** The tools the server listed when last checked. */
function ToolList({ server }: { server: McpServerRecord }) {
  return (
    <ul className="flex flex-col divide-y rounded-(--radius-item) border">
      {server.tools.map((tool) => (
        <li key={tool.name} className="flex flex-col gap-0.5 px-3 py-2">
          <span className="font-mono text-2xs text-foreground">
            {tool.name}
            {tool.title && (
              <span className="ml-1.5 font-sans text-muted-foreground">
                {tool.title}
              </span>
            )}
          </span>
          {tool.description && (
            <span className="line-clamp-2 text-2xs text-muted-foreground">
              {tool.description}
            </span>
          )}
        </li>
      ))}
    </ul>
  )
}
