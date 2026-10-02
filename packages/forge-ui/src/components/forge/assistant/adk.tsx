import * as React from "react"
import {
  useAuiState,
  type ToolCallMessagePartComponent,
} from "@assistant-ui/react"
import {
  useAdkAgentInfo,
  useAdkArtifacts,
  useAdkAuthRequests,
  useAdkConfirmTool,
  useAdkEscalation,
  useAdkSubmitAuth,
  useAdkSubmitInput,
  type AdkAuthCredential,
} from "@assistant-ui/react-google-adk"
import {
  Cancel01Icon,
  SecurityCheckIcon,
  Tick02Icon,
} from "@hugeicons/core-free-icons"
import { cn } from "cn"

import { ToolFallback } from "@/components/assistant-ui/elements/tool-fallback.aui"
import { Button } from "@/components/ui/button"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { Input } from "@/components/ui/input"
import { Spinner } from "@/components/ui/spinner"
import { AgentOrb } from "@/components/forge/avatars"
import { Icon } from "@/components/forge/icon"
import type { IconProp } from "@/components/forge/icons"
import { CountBadge } from "@/components/forge/status"

import { useAssistantSettings, type ApprovalView } from "./assistant-context"
import { extendedThinkingLevels } from "./model-settings"
import {
  ModelSelectorRoot,
  ModelSelectorTrigger,
  ModelSelectorContent,
  ModelSelectorSearch,
  ModelSelectorList,
  ModelSelectorEmpty,
  ModelSelectorGroup,
  ModelSelectorItem,
  ModelSelectorEffort,
  type ModelOption,
} from "@/components/assistant-ui/elements/model-selector"

/* -------------------------------------------------------------------------- */
/* Shared                                                                     */
/* -------------------------------------------------------------------------- */

/** "search_agent" → "Search agent". */
const humanize = (name: string) =>
  name.replace(/[_-]+/g, " ").replace(/^\w/, (c) => c.toUpperCase())

/** What an agent is called: its name in `agents`, or its own, humanized. */
function useAgentName() {
  const { agents } = useAssistantSettings()
  return React.useCallback(
    (name: string) =>
      (Object.hasOwn(agents ?? {}, name) ? agents?.[name] : undefined) ??
      humanize(name),
    [agents]
  )
}

/** Stable orb gradient per agent name. */
const orbVariant = (name: string) =>
  [...name].reduce((sum, char) => sum + char.charCodeAt(0), 0)

/** ADK messages use markdown code spans; render them as inline code. */
function InlineText({ text }: { text: string }) {
  return text.split(/(`[^`]+`)/).map((piece, index) =>
    piece.startsWith("`") && piece.endsWith("`") ? (
      <code key={index} className="font-mono text-foreground">
        {piece.slice(1, -1)}
      </code>
    ) : (
      piece
    )
  )
}

/** Tool results arrive as JSON text or values; unwrap ADK's `{ result }`. */
const unwrapResult = (result: unknown): unknown => {
  let value = result
  if (typeof value === "string") {
    try {
      value = JSON.parse(value)
    } catch {
      return value
    }
  }
  return typeof value === "object" && value !== null && "result" in value
    ? (value as { result: unknown }).result
    : value
}

/** A human-in-the-loop request, inline in the conversation. */
function RequestCard({
  icon,
  title,
  description,
  done,
  children,
}: {
  icon: IconProp
  title: React.ReactNode
  description?: React.ReactNode
  done?: React.ReactNode
  children?: React.ReactNode
}) {
  return (
    <div
      data-slot="adk-request"
      className="my-2 flex flex-col gap-3 rounded-(--radius-card) border border-border bg-card p-3.5 text-xs"
    >
      <div className="flex items-start gap-2.5">
        <span className="flex size-7 shrink-0 items-center justify-center rounded-(--radius-soft) bg-muted text-muted-foreground">
          <Icon icon={done ? "check" : icon} size={15} />
        </span>
        <div className="flex min-w-0 flex-1 flex-col gap-0.5 pt-px">
          <p className="font-medium text-foreground">{title}</p>
          {description && (
            <p className="leading-relaxed text-muted-foreground">
              {description}
            </p>
          )}
        </div>
      </div>
      {done ? (
        <p className="flex items-center gap-1.5 text-muted-foreground">
          {done}
        </p>
      ) : (
        children
      )}
    </div>
  )
}

/* -------------------------------------------------------------------------- */
/* Auth requests (adk_request_credential)                                     */
/* -------------------------------------------------------------------------- */

const authUriOf = (authConfig: unknown) =>
  (authConfig as { exchangedAuthCredential?: AdkAuthCredential } | undefined)
    ?.exchangedAuthCredential?.oauth2?.authUri

const hostOf = (uri: string | undefined) => {
  if (!uri) return undefined
  try {
    return new URL(uri).host
  } catch {
    return undefined
  }
}

/**
 * Renders ADK's `adk_request_credential` call: a sign-in card that hands the
 * request to `onAuthRequest` and submits the credential it resolves with.
 */
export const AdkAuthRequestUI: ToolCallMessagePartComponent<
  { authConfig?: unknown; auth_config?: unknown },
  unknown
> = ({ toolCallId, args, result }) => {
  const { onAuthRequest } = useAssistantSettings()
  const requests = useAdkAuthRequests()
  const submitAuth = useAdkSubmitAuth()
  const [pending, setPending] = React.useState(false)
  const [error, setError] = React.useState<string>()

  const request = requests.find((r) => r.toolCallId === toolCallId)
  // The call's own arguments carry the auth config after it's answered, too.
  const authUri = authUriOf(
    request?.authConfig ?? args.authConfig ?? args.auth_config
  )
  const host = hostOf(authUri)

  const signIn = async () => {
    if (!request || !onAuthRequest) return
    setPending(true)
    setError(undefined)
    try {
      const credential = await onAuthRequest({ ...request, authUri })
      await submitAuth(toolCallId, credential)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Sign-in failed.")
    } finally {
      setPending(false)
    }
  }

  return (
    <RequestCard
      icon="link"
      title="Sign-in required"
      description={
        host
          ? `The agent needs access to ${host} to continue.`
          : "The agent needs you to sign in to continue."
      }
      done={
        result !== undefined || (!request && !pending) ? (
          <>Signed in{host ? ` to ${host}` : ""}.</>
        ) : undefined
      }
    >
      <div className="flex flex-wrap items-center gap-2">
        <Button size="sm" disabled={pending || !onAuthRequest} onClick={signIn}>
          {pending && <Spinner data-icon="inline-start" />}
          {pending ? "Waiting for sign-in…" : "Sign in"}
        </Button>
        {!onAuthRequest && (
          <span className="text-muted-foreground">
            Pass <code>onAuthRequest</code> to complete sign-in.
          </span>
        )}
        {error && <span className="text-destructive">{error}</span>}
      </div>
    </RequestCard>
  )
}

/* -------------------------------------------------------------------------- */
/* Input requests (adk_request_input)                                         */
/* -------------------------------------------------------------------------- */

type RequestInputArgs = {
  message?: string
  response_schema?: { type?: string; enum?: unknown[] }
}

/**
 * Renders ADK's `adk_request_input` call (Workflow `RequestInput` nodes):
 * choice buttons for an enum or boolean schema, a text field otherwise.
 */
export const AdkInputRequestUI: ToolCallMessagePartComponent<
  RequestInputArgs,
  unknown
> = ({ toolCallId, args, result }) => {
  const submitInput = useAdkSubmitInput()
  const [value, setValue] = React.useState("")
  const [pending, setPending] = React.useState(false)
  const schema = args.response_schema
  const choices =
    schema?.enum?.map(String) ??
    (schema?.type === "boolean" ? ["Yes", "No"] : undefined)

  const submit = async (answer: string) => {
    setPending(true)
    try {
      const typed = schema?.type === "boolean" ? answer === "Yes" : answer
      await submitInput(toolCallId, typed)
    } finally {
      setPending(false)
    }
  }

  const answered = result === undefined ? undefined : unwrapResult(result)
  const answer =
    answered === undefined
      ? undefined
      : answered === true
        ? "Yes"
        : answered === false
          ? "No"
          : String(answered)

  return (
    <RequestCard
      icon="message"
      title="The agent has a question"
      description={
        <InlineText
          text={args.message ?? "Please provide input to continue."}
        />
      }
      done={answer !== undefined ? <>Answered: {answer}</> : undefined}
    >
      {choices ? (
        <div className="flex flex-wrap gap-2">
          {choices.map((choice) => (
            <Button
              key={choice}
              size="sm"
              variant="outline"
              disabled={pending}
              onClick={() => submit(choice)}
            >
              {choice}
            </Button>
          ))}
        </div>
      ) : (
        <form
          className="flex items-center gap-2"
          onSubmit={(event) => {
            event.preventDefault()
            if (value.trim()) void submit(value.trim())
          }}
        >
          <Input
            value={value}
            onChange={(event) => setValue(event.target.value)}
            placeholder="Your answer"
            aria-label={args.message ?? "Your answer"}
            className="h-8 text-xs"
            disabled={pending}
          />
          <Button type="submit" size="sm" disabled={pending || !value.trim()}>
            {pending && <Spinner data-icon="inline-start" />}
            Send
          </Button>
        </form>
      )}
    </RequestCard>
  )
}

/* -------------------------------------------------------------------------- */
/* Tool confirmations (adk_request_confirmation)                              */
/* -------------------------------------------------------------------------- */

type ConfirmationArgs = {
  originalFunctionCall?: { name?: string; args?: Record<string, unknown> }
  original_function_call?: { name?: string; args?: Record<string, unknown> }
  toolConfirmation?: { hint?: string }
  tool_confirmation?: { hint?: string }
}

const formatArg = (value: unknown) =>
  typeof value === "string" ? value : JSON.stringify(value)

// ADK's own hint, from `require_confirmation`, is written for the client
// that answers it, not for the person; the card says it in its own words.
const ADK_PROTOCOL_HINT = /^Please approve or reject the tool call /

/** A small pulsing dot: this is waiting on the person. */
function WaitingDot() {
  return (
    <span aria-hidden className="relative flex size-1.5">
      <span className="absolute inline-flex size-full animate-ping rounded-full bg-warning-foreground opacity-60 motion-reduce:animate-none" />
      <span className="relative inline-flex size-1.5 rounded-full bg-warning-foreground" />
    </span>
  )
}

/** The call's arguments, when the tool has no preview of its own. */
function ArgumentList({ args }: { args: Record<string, unknown> }) {
  const entries = Object.entries(args)
  if (entries.length === 0) return null
  return (
    <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 rounded-(--radius-soft) border border-border/60 bg-muted/60 px-3 py-2 font-mono text-2xs">
      {entries.map(([key, value]) => (
        <React.Fragment key={key}>
          <dt className="text-muted-foreground">{key}</dt>
          <dd className="min-w-0 break-all text-foreground">
            {formatArg(value)}
          </dd>
        </React.Fragment>
      ))}
    </dl>
  )
}

/** Once decided, the card folds to one line saying what the person chose. */
function ApprovalOutcome({
  approved,
  toolName,
  view,
}: {
  approved: boolean
  toolName: string
  view: ApprovalView | undefined
}) {
  return (
    <p
      data-slot="adk-approval-outcome"
      className="my-1 flex animate-in items-center gap-2 text-xs text-muted-foreground duration-300 fade-in"
    >
      <span
        className={cn(
          "flex size-4 shrink-0 items-center justify-center rounded-full",
          approved
            ? "bg-success-surface text-success-foreground"
            : "bg-muted text-muted-foreground"
        )}
      >
        <Icon icon={approved ? Tick02Icon : Cancel01Icon} size={10} />
      </span>
      {approved
        ? (view?.approvedLabel ?? (
            <>
              You approved <code className="font-mono">{toolName}</code>
            </>
          ))
        : (view?.deniedLabel ?? (
            <>
              You denied <code className="font-mono">{toolName}</code>
            </>
          ))}
    </p>
  )
}

/**
 * Renders ADK's `adk_request_confirmation` call, which a tool with
 * `require_confirmation` makes before it runs: a card saying what the agent
 * wants to do (the tool's `ApprovalView` in `approvals`, else its name and
 * arguments), with Approve / Deny. The run waits until the person decides;
 * the card then folds to the outcome. The gated call itself renders without
 * controls (see `AdkToolFallback`), so the decision is made in one place.
 */
export const AdkConfirmationUI: ToolCallMessagePartComponent<
  ConfirmationArgs,
  unknown
> = ({ toolCallId, args, approval }) => {
  const confirmTool = useAdkConfirmTool()
  const { approvals } = useAssistantSettings()
  const [pending, setPending] = React.useState<boolean>()
  const [error, setError] = React.useState<string>()
  const original = args.originalFunctionCall ?? args.original_function_call
  const toolName = original?.name ?? "tool"
  const callArgs = original?.args ?? {}
  const view = approvals?.[toolName]
  const hint = (args.toolConfirmation ?? args.tool_confirmation)?.hint
  const Preview = view?.preview

  if (approval?.approved !== undefined)
    return (
      <ApprovalOutcome
        approved={approval.approved}
        toolName={toolName}
        view={view}
      />
    )

  const decide = async (confirmed: boolean) => {
    setPending(confirmed)
    setError(undefined)
    try {
      await confirmTool(toolCallId, confirmed)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      setPending(undefined)
    }
  }

  return (
    <section
      data-slot="adk-approval"
      aria-label="Needs your approval"
      className="my-2 animate-in overflow-hidden rounded-(--radius-card) border border-warning-foreground/25 bg-card shadow-sm duration-300 fade-in slide-in-from-bottom-1"
    >
      <header className="flex items-center gap-2 border-b border-warning-foreground/15 bg-warning-surface px-3.5 py-2 text-xs text-warning-foreground">
        <Icon icon={SecurityCheckIcon} size={14} />
        <span className="font-medium">Needs your approval</span>
        <span className="ms-auto flex items-center gap-1.5 opacity-90">
          <WaitingDot />
          Waiting for you
        </span>
      </header>
      <div className="flex flex-col gap-3 p-3.5">
        <div className="flex flex-col gap-1">
          <p className="text-sm font-medium text-foreground">
            {view?.title ?? (
              <>
                Run <code className="font-mono">{toolName}</code>?
              </>
            )}
          </p>
          <p className="text-xs leading-relaxed text-muted-foreground">
            {view?.description ??
              (hint && !ADK_PROTOCOL_HINT.test(hint) ? (
                <InlineText text={hint} />
              ) : (
                "The assistant wants to run this tool. Nothing happens until you approve it."
              ))}
          </p>
        </div>
        {Preview ? (
          <Preview args={callArgs} />
        ) : (
          <ArgumentList args={callArgs} />
        )}
        <div className="flex flex-wrap items-center gap-2">
          <Button
            size="sm"
            disabled={pending !== undefined}
            onClick={() => decide(true)}
          >
            {pending === true ? (
              <Spinner data-icon="inline-start" />
            ) : (
              <Icon icon={Tick02Icon} data-icon="inline-start" />
            )}
            {view?.approveLabel ?? "Approve"}
          </Button>
          <Button
            size="sm"
            variant="ghost"
            disabled={pending !== undefined}
            onClick={() => decide(false)}
          >
            {pending === false && <Spinner data-icon="inline-start" />}
            Deny
          </Button>
          {error && <span className="text-xs text-destructive">{error}</span>}
        </div>
      </div>
    </section>
  )
}

const textOf = (value: unknown, key: string) => {
  if (typeof value === "string") return value
  const field =
    value && typeof value === "object"
      ? (value as Record<string, unknown>)[key]
      : undefined
  return typeof field === "string" ? field : undefined
}

/**
 * A call handing a request to one of the app's agents (`agents`), e.g. an
 * ADK supervisor's specialist: what it was asked and what it reported. It
 * shows in the message rather than folded into the tool calls, so the
 * person sees who is working on it.
 */
export const AdkDelegation: ToolCallMessagePartComponent = ({
  toolName,
  args,
  argsText,
  result,
  status,
}) => {
  const agentName = useAgentName()
  const request = textOf(args, "request")
  const report = textOf(result, "result")
  return (
    <ToolFallback.Root data-slot="adk-delegation">
      <ToolFallback.Trigger
        toolName={toolName}
        status={status}
        label={
          <>
            Asked <b>{agentName(toolName)}</b>
          </>
        }
      />
      <ToolFallback.Content>
        <ToolFallback.Error status={status} />
        {request !== undefined ? (
          <p className="whitespace-pre-line text-muted-foreground">{request}</p>
        ) : (
          <ToolFallback.Args argsText={argsText} />
        )}
        {report !== undefined ? (
          <div className="flex flex-col gap-1">
            <p className="text-xs font-medium text-muted-foreground">
              Reported:
            </p>
            <p className="whitespace-pre-line text-foreground/90">{report}</p>
          </div>
        ) : (
          <ToolFallback.Result result={result} />
        )}
      </ToolFallback.Content>
    </ToolFallback.Root>
  )
}

/**
 * `ToolFallback` for ADK: a call gated by a confirmation shows its trigger,
 * arguments and result but no approval controls, since
 * `AdkConfirmationUI` owns the decision. Other calls render as usual.
 */
export const AdkToolFallback: ToolCallMessagePartComponent = (props) => {
  if (!props.approval) return <ToolFallback {...props} />
  return (
    <ToolFallback.Root>
      <ToolFallback.Trigger toolName={props.toolName} status={props.status} />
      <ToolFallback.Content>
        <ToolFallback.Error status={props.status} />
        <ToolFallback.Args argsText={props.argsText} />
        <ToolFallback.Result result={props.result} />
      </ToolFallback.Content>
    </ToolFallback.Root>
  )
}

/* -------------------------------------------------------------------------- */
/* Session-level status                                                       */
/* -------------------------------------------------------------------------- */

/** The agent currently answering (multi-agent apps), for the top bar. */
function AdkAgentIndicator({ className }: { className?: string }) {
  const agent = useAdkAgentInfo()
  const agentName = useAgentName()
  if (!agent?.name) return null
  return (
    <span
      data-slot="adk-agent"
      title={agent.branch}
      className={cn(
        "flex shrink-0 items-center gap-1.5 text-xs whitespace-nowrap text-muted-foreground",
        className
      )}
    >
      <AgentOrb variant={orbVariant(agent.name)} />
      <span className="text-foreground">{agentName(agent.name)}</span>
    </span>
  )
}

const authorOf = (metadata: { custom?: Record<string, unknown> }) =>
  typeof metadata.custom?.author === "string"
    ? metadata.custom.author
    : undefined

/**
 * The agent that wrote an assistant message, for multi-agent conversations:
 * shown where the speaking agent changes, hidden while only one agent talks.
 */
function AdkMessageAuthor() {
  const agentName = useAgentName()
  const author = useAuiState((s) => authorOf(s.message.metadata))
  const changed = useAuiState((s) => {
    for (let index = s.message.index - 1; index >= 0; index--) {
      const previous = s.thread.messages[index]
      if (previous?.role === "assistant") {
        return authorOf(previous.metadata) !== authorOf(s.message.metadata)
      }
    }
    return true
  })
  const multiAgent = useAuiState(
    (s) =>
      new Set(
        s.thread.messages
          .filter((m) => m.role === "assistant")
          .map((m) => authorOf(m.metadata))
          .filter(Boolean)
      ).size > 1
  )
  if (!author || !multiAgent || !changed) return null
  return (
    <div
      data-slot="adk-message-author"
      className="mb-1.5 flex items-center gap-1.5 text-2xs text-muted-foreground"
    >
      <AgentOrb variant={orbVariant(author)} className="size-3.5" />
      {agentName(author)}
    </div>
  )
}

/** Shown once the agent escalates the conversation to a person. */
function AdkEscalationBanner({
  children = "The agent handed this conversation to a person. They'll reply here.",
  className,
}: {
  children?: React.ReactNode
  className?: string
}) {
  const escalated = useAdkEscalation()
  if (!escalated) return null
  return (
    <div
      role="status"
      data-slot="adk-escalation"
      className={cn(
        "flex items-center gap-2 border-b border-notice-border bg-notice-surface px-5 py-2 text-xs text-notice-foreground",
        className
      )}
    >
      <Icon icon="team" size={15} />
      {children}
    </div>
  )
}

const downloadArtifact = (
  name: string,
  data: {
    text?: string
    inlineData?: { data: string; mimeType: string }
    fileData?: { fileUri: string }
  }
) => {
  if (data.fileData?.fileUri) {
    window.open(data.fileData.fileUri, "_blank", "noopener")
    return
  }
  const blob = data.inlineData
    ? new Blob(
        [Uint8Array.from(atob(data.inlineData.data), (c) => c.charCodeAt(0))],
        { type: data.inlineData.mimeType }
      )
    : new Blob([data.text ?? ""], { type: "text/plain" })
  const url = URL.createObjectURL(blob)
  const link = Object.assign(document.createElement("a"), {
    href: url,
    download: name,
  })
  link.click()
  URL.revokeObjectURL(url)
}

/**
 * Files the agent saved in this session. Downloads need `artifacts` from
 * `useAdkAssistant` (direct ADK connections); otherwise they're listed only.
 * `compact` drops the label, for tight headers.
 */
function AdkArtifactsMenu({ compact = false }: { compact?: boolean }) {
  const artifacts = useAdkArtifacts()
  const { artifacts: api, onOpenArtifact } = useAssistantSettings()
  const sessionId = useAuiState(
    (s) =>
      s.threads.threadItems.find((t) => t.id === s.threads.mainThreadId)
        ?.remoteId
  )
  const names = Object.keys(artifacts)
  if (names.length === 0) return null

  return (
    <DropdownMenu>
      <DropdownMenuTrigger
        render={
          compact ? (
            <Button
              variant="ghost"
              size="sm"
              className="h-7 gap-1 px-1.5 text-muted-foreground"
            />
          ) : (
            <Button variant="outline" size="sm" />
          )
        }
        aria-label={`Artifacts (${names.length})`}
      >
        <Icon icon="file" data-icon="inline-start" />
        {!compact && "Artifacts"}
        <CountBadge>{names.length}</CountBadge>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="min-w-56">
        <DropdownMenuGroup>
          <DropdownMenuLabel>Saved by the agent</DropdownMenuLabel>
          {names.map((name) => (
            <DropdownMenuItem
              key={name}
              disabled={!onOpenArtifact && (!api || !sessionId)}
              onClick={async () => {
                if (onOpenArtifact) return onOpenArtifact(name)
                if (!api || !sessionId) return
                downloadArtifact(name, await api.load(sessionId, name))
              }}
            >
              <Icon icon="file" />
              <span className="min-w-0 flex-1 truncate">{name}</span>
              <span className="text-2xs text-muted-foreground">
                v{artifacts[name]}
              </span>
            </DropdownMenuItem>
          ))}
        </DropdownMenuGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

/* -------------------------------------------------------------------------- */
/* Composer model section                                                     */
/* -------------------------------------------------------------------------- */

// Matches the composer's own round controls.
const composerChip =
  "h-7 min-w-0 gap-1.5 rounded-full px-2.5 font-normal text-muted-foreground hover:bg-muted-foreground/15 hover:text-foreground data-popup-open:bg-muted-foreground/15 data-popup-open:text-foreground"

/** The ghost model picker, bound to the existing ADK selection and run state. */
function AdkModelSection({ className }: { className?: string }) {
  const { modelSettings } = useAssistantSettings()
  const show = modelSettings?.show
  React.useEffect(() => show?.(), [show])
  if (!modelSettings) return null

  const {
    models,
    model,
    setModel,
    thinkingLevels,
    thinkingLevel,
    setThinkingLevel,
  } = modelSettings
  const options: ModelOption[] = models.map((option) => {
    const levels = option.thinkingLevels
      ? option.thinkingLevels.map((id) => ({
          id,
          name:
            extendedThinkingLevels.find((level) => level.id === id)?.label ??
            humanize(id),
        }))
      : thinkingLevels.map((level) => ({ id: level.id, name: level.label }))
    return {
      id: option.id,
      name: option.name,
      description: option.description,
      icon: option.icon ? <Icon icon={option.icon} /> : undefined,
      keywords: option.group ? [option.group] : [],
      efforts:
        levels.length > 1
          ? levels.map((level) => ({
              ...level,
              name: level.id === "medium" ? "Med" : level.name,
            }))
          : undefined,
    }
  })
  const groups = [...new Set(models.map((option) => option.group))]
  return (
    <div data-slot="adk-model-section" className={cn("shrink-0", className)}>
      <ModelSelectorRoot
        models={options}
        value={model}
        onValueChange={setModel}
        effort={thinkingLevel}
        onEffortChange={setThinkingLevel}
      >
        <ModelSelectorTrigger variant="ghost" size="sm" className="h-7" />
        <ModelSelectorContent
          side="top"
          align="start"
          className="w-80 max-w-[calc(100vw-2rem)]"
        >
          <ModelSelectorSearch />
          <ModelSelectorList>
            <ModelSelectorEmpty />
            {groups.map((group) => (
              <ModelSelectorGroup key={group ?? "models"} heading={group}>
                {options
                  .filter(
                    (option) =>
                      models.find((model) => model.id === option.id)?.group ===
                      group
                  )
                  .map((option) => (
                    <ModelSelectorItem key={option.id} model={option} />
                  ))}
              </ModelSelectorGroup>
            ))}
          </ModelSelectorList>
          <ModelSelectorEffort />
        </ModelSelectorContent>
      </ModelSelectorRoot>
    </div>
  )
}

export {
  composerChip,
  AdkAgentIndicator,
  AdkArtifactsMenu,
  AdkEscalationBanner,
  AdkMessageAuthor,
  AdkModelSection,
}
