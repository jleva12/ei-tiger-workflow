import * as React from "react"
import {
  ThreadPrimitive,
  useAuiState,
  type ToolCallMessagePartComponent,
} from "@assistant-ui/react"
import {
  useAdkAgentInfo,
  useAdkArtifacts,
  useAdkAuthRequests,
  useAdkConfirmTool,
  useAdkEscalation,
  useAdkLongRunningToolIds,
  useAdkSessionState,
  useAdkSubmitAuth,
  useAdkSubmitInput,
  type AdkAuthCredential,
} from "@assistant-ui/react-google-adk"
import { AiBrain01Icon } from "@hugeicons/core-free-icons"
import { cn } from "cn"

import { ThreadFollowupSuggestions } from "@/components/assistant-ui/elements/follow-up-suggestions.aui"
import { ToolFallback } from "@/components/assistant-ui/elements/tool-fallback.aui"
import { Button } from "@/components/ui/button"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover"
import {
  Questionnaire,
  QuestionnaireActions,
  QuestionnaireChoice,
  QuestionnaireChoices,
  QuestionnaireDescription,
  QuestionnaireError,
  QuestionnaireInput,
  QuestionnaireItem,
  QuestionnaireNext,
  QuestionnairePrevious,
  QuestionnaireProgress,
  QuestionnaireSkip,
  QuestionnaireSubmit,
  QuestionnaireTitle,
} from "@/components/ui/questionnaire"
import { Spinner } from "@/components/ui/spinner"
import { AgentOrb } from "@/components/forge/avatars"
import { Icon } from "@/components/forge/icon"
import type { IconProp } from "@/components/forge/icons"
import { CountBadge } from "@/components/forge/status"

import { useAssistantSettings } from "./assistant-context"
import { SearchLabel, type AssistantSearch } from "./sources"
import { answerFrom, askForm, describeAnswer } from "./ask-person"
import { followUpsOf } from "./follow-ups"
import {
  compactTokens,
  contextView,
  currentContext,
  fullTokens,
  percent,
  type ContextView,
} from "./context-window"
import type { AssistantModel } from "./model-settings"

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
  plain = false,
  children,
}: {
  icon: IconProp
  title: React.ReactNode
  description?: React.ReactNode
  done?: React.ReactNode
  /** Part of the reply, without the card's outline and fill. */
  plain?: boolean
  children?: React.ReactNode
}) {
  return (
    <div
      data-slot="adk-request"
      data-plain={plain || undefined}
      className={cn(
        "my-2 flex flex-col gap-3 text-xs",
        !plain && "rounded-(--radius-card) border border-border bg-card p-3.5"
      )}
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
  response_schema?: unknown
}

/**
 * Renders ADK's `adk_request_input` call (an agent asking the person, or a
 * Workflow `RequestInput` node) as a form: a `Questionnaire` with a question
 * per field of its `response_schema` (one at a time, with progress), or one
 * question for any other schema: pick one, pick several, yes or no, or a typed
 * answer (text, number, email, URL, date…). The answer goes back in the
 * schema's types (`answerFrom`), and the turn carries on.
 */
export const AdkInputRequestUI: ToolCallMessagePartComponent<
  RequestInputArgs,
  unknown
> = ({ toolCallId, args, result }) => {
  const submitInput = useAdkSubmitInput()
  const [pending, setPending] = React.useState(false)
  const message = args.message?.trim() || "Please provide input to continue."
  const form = React.useMemo(
    () => askForm(args.response_schema, message),
    [args.response_schema, message]
  )
  const several = form.questions.length > 1

  const submit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    const answer = answerFrom(form, new FormData(event.currentTarget))
    setPending(true)
    try {
      await submitInput(toolCallId, answer)
    } finally {
      setPending(false)
    }
  }

  const answered = result === undefined ? undefined : unwrapResult(result)
  return (
    <RequestCard
      icon="message"
      title="The agent has a question"
      plain
      // One question is its own title; a form's questions are its fields.
      description={form.fields ? <InlineText text={message} /> : undefined}
      done={
        answered !== undefined ? (
          <>Answered: {describeAnswer(answered, form)}</>
        ) : undefined
      }
    >
      <Questionnaire
        onSubmit={submit}
        aria-busy={pending}
        aria-label={message}
        className="gap-3"
      >
        {several && <QuestionnaireProgress />}
        {form.questions.map((question) => (
          <QuestionnaireItem
            key={question.name}
            name={question.name}
            required={question.required}
            multiple={question.kind === "multiple"}
            disabled={pending}
          >
            <QuestionnaireTitle>
              {form.fields ? (
                question.title
              ) : (
                <InlineText text={question.title} />
              )}
            </QuestionnaireTitle>
            {question.description && (
              <QuestionnaireDescription>
                {question.description}
              </QuestionnaireDescription>
            )}
            {question.choices ? (
              <QuestionnaireChoices>
                {question.choices.map((choice) => (
                  <QuestionnaireChoice
                    key={choice.value}
                    value={choice.value}
                    defaultChecked={question.defaults.includes(choice.value)}
                  >
                    {choice.label}
                  </QuestionnaireChoice>
                ))}
              </QuestionnaireChoices>
            ) : (
              <QuestionnaireInput
                type={question.input?.type}
                placeholder={question.input?.placeholder}
                min={question.input?.min}
                max={question.input?.max}
                step={question.input?.step}
                defaultValue={question.defaults[0]}
                aria-label={question.title}
              />
            )}
            <QuestionnaireError />
          </QuestionnaireItem>
        ))}
        <QuestionnaireActions>
          {several && <QuestionnairePrevious />}
          <QuestionnaireSkip />
          {several && <QuestionnaireNext />}
          <QuestionnaireSubmit disabled={pending}>
            {pending && <Spinner data-icon="inline-start" />}
            Send
          </QuestionnaireSubmit>
        </QuestionnaireActions>
      </Questionnaire>
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

/**
 * Renders ADK's `adk_request_confirmation` call: the gated tool, its
 * arguments and ADK's hint, with Approve / Deny. The gated call itself
 * renders without controls (see `AdkToolFallback`), so the decision is made
 * in one place.
 */
export const AdkConfirmationUI: ToolCallMessagePartComponent<
  ConfirmationArgs,
  unknown
> = ({ toolCallId, args, approval }) => {
  const confirmTool = useAdkConfirmTool()
  const [pending, setPending] = React.useState<boolean>()
  const [error, setError] = React.useState<string>()
  const original = args.originalFunctionCall ?? args.original_function_call
  const hint = (args.toolConfirmation ?? args.tool_confirmation)?.hint
  const entries = Object.entries(original?.args ?? {})

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
    <RequestCard
      icon="warning"
      title={
        <>
          Approve <code className="font-mono">{original?.name ?? "tool"}</code>?
        </>
      }
      description={
        <InlineText text={hint || "The agent wants to run this tool."} />
      }
      done={
        approval?.approved === true
          ? "Approved."
          : approval?.approved === false
            ? "Denied."
            : undefined
      }
    >
      {entries.length > 0 && (
        <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 rounded-(--radius-soft) bg-muted px-2.5 py-2 font-mono text-2xs">
          {entries.map(([key, value]) => (
            <React.Fragment key={key}>
              <dt className="text-muted-foreground">{key}</dt>
              <dd className="min-w-0 break-all text-foreground">
                {formatArg(value)}
              </dd>
            </React.Fragment>
          ))}
        </dl>
      )}
      <div className="flex flex-wrap items-center gap-2">
        <Button
          size="sm"
          disabled={pending !== undefined}
          onClick={() => decide(true)}
        >
          {pending === true && <Spinner data-icon="inline-start" />}
          Approve
        </Button>
        <Button
          size="sm"
          variant="outline"
          disabled={pending !== undefined}
          onClick={() => decide(false)}
        >
          {pending === false && <Spinner data-icon="inline-start" />}
          Deny
        </Button>
        {error && <span className="text-destructive">{error}</span>}
      </div>
    </RequestCard>
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

/** The search a finished tool call made, as the sources config reads it. */
function useToolSearch(
  toolName: string,
  result: unknown
): AssistantSearch | undefined {
  const searchOf = useAssistantSettings().sources?.searchOf
  return React.useMemo(() => {
    if (!searchOf || result === undefined) return undefined
    try {
      return searchOf(toolName, result)
    } catch {
      return undefined // a result the config can't read is just a tool call
    }
  }, [searchOf, toolName, result])
}

/**
 * `ToolFallback` for ADK: a call gated by a confirmation shows its trigger,
 * arguments and result but no approval controls, since
 * `AdkConfirmationUI` owns the decision. A search (the sources config's
 * `searchOf`) says what it searched and found. Other calls render as usual.
 */
export const AdkToolFallback: ToolCallMessagePartComponent = (props) => {
  const search = useToolSearch(props.toolName, props.result)
  if (!props.approval && !search) return <ToolFallback {...props} />
  return (
    <ToolFallback.Root>
      <ToolFallback.Trigger
        toolName={props.toolName}
        status={props.status}
        label={search && <SearchLabel search={search} />}
      />
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
  const { artifacts: api } = useAssistantSettings()
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
              disabled={!api || !sessionId}
              onClick={async () => {
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

/** Models by their `group`, in the order the groups first appear. */
function modelGroups(models: AssistantModel[]) {
  const groups = new Map<string | undefined, AssistantModel[]>()
  for (const model of models) {
    const group = groups.get(model.group)
    if (group) group.push(model)
    else groups.set(model.group, [model])
  }
  return [...groups].map(([name, members]) => ({ name, models: members }))
}

function OptionText({
  label,
  description,
}: {
  label: string
  description?: string | undefined
}) {
  return (
    <span className="flex min-w-0 flex-col gap-0.5">
      <span>{label}</span>
      {description && (
        <span className="text-2xs text-muted-foreground">{description}</span>
      )}
    </span>
  )
}

/**
 * The composer's model section (`AssistantScreen` `showModels`): the agent
 * answering, the model it runs on and how long it thinks. The selection goes
 * to the agent as ADK state with every run; see `useAdkAssistant`.
 */
function AdkModelSection({ className }: { className?: string }) {
  const { modelSettings, title } = useAssistantSettings()
  const agent = useAdkAgentInfo()
  const nameOf = useAgentName()
  const show = modelSettings?.show
  React.useEffect(() => show?.(), [show])
  if (!modelSettings) return null

  const { models, model, setModel, thinkingLevels, setThinkingLevel } =
    modelSettings
  const agentName = agent?.name ? nameOf(agent.name) : (title ?? "Assistant")
  const current = models.find((option) => option.id === model)
  // Models without an icon keep their names in line with those that have one.
  const withIcons = models.some((option) => option.icon)
  const thinking = thinkingLevels.find(
    (level) => level.id === modelSettings.thinkingLevel
  )

  const agentLabel = (
    <>
      <AgentOrb variant={orbVariant(agentName)} className="size-3.5" />
      <span className="truncate text-foreground">{agentName}</span>
      {current && (
        <span className="truncate @max-[30rem]:hidden">{current.name}</span>
      )}
    </>
  )

  return (
    <div
      data-slot="adk-model-section"
      className={cn("flex min-w-0 items-center gap-0.5 text-xs", className)}
    >
      {models.length > 1 ? (
        <DropdownMenu>
          <DropdownMenuTrigger
            render={
              // It gives way first (its name truncates), so the thinking
              // level and the context meter beside it never overlap it.
              <Button
                variant="ghost"
                size="xs"
                className={cn(composerChip, "shrink")}
              />
            }
            aria-label={`Model: ${current?.name ?? agentName}`}
          >
            {agentLabel}
            <Icon icon="down" data-icon="inline-end" />
          </DropdownMenuTrigger>
          <DropdownMenuContent side="top" align="start" className="w-64">
            <DropdownMenuGroup>
              <DropdownMenuLabel>{agentName} runs on</DropdownMenuLabel>
            </DropdownMenuGroup>
            {modelGroups(models).map((group, index) => (
              <React.Fragment key={group.name ?? ""}>
                {index > 0 && <DropdownMenuSeparator />}
                <DropdownMenuGroup>
                  {group.name && (
                    <DropdownMenuLabel className="font-medium text-foreground">
                      {group.name}
                    </DropdownMenuLabel>
                  )}
                  {/* One radio group per model group, all bound to the model. */}
                  <DropdownMenuRadioGroup
                    value={model}
                    onValueChange={(value) => setModel(String(value))}
                  >
                    {group.models.map((option) => (
                      <DropdownMenuRadioItem
                        closeOnClick
                        key={option.id}
                        value={option.id}
                      >
                        {option.icon ? (
                          <Icon icon={option.icon} />
                        ) : (
                          withIcons && <span aria-hidden className="size-4" />
                        )}
                        <OptionText
                          label={option.name}
                          description={option.description}
                        />
                      </DropdownMenuRadioItem>
                    ))}
                  </DropdownMenuRadioGroup>
                </DropdownMenuGroup>
              </React.Fragment>
            ))}
          </DropdownMenuContent>
        </DropdownMenu>
      ) : (
        <span className="flex h-7 min-w-0 items-center gap-1.5 px-2.5 text-muted-foreground">
          {agentLabel}
        </span>
      )}
      {/* A model that doesn't reason has nothing to choose. */}
      {thinking && thinkingLevels.length > 1 && (
        <DropdownMenu>
          <DropdownMenuTrigger
            render={
              <Button variant="ghost" size="xs" className={composerChip} />
            }
            aria-label={`Thinking: ${thinking.label}`}
          >
            <Icon icon={AiBrain01Icon} data-icon="inline-start" />
            <span className="@max-[30rem]:hidden">Thinking</span>
            <span className="text-foreground">{thinking.label}</span>
            <Icon icon="down" data-icon="inline-end" />
          </DropdownMenuTrigger>
          <DropdownMenuContent side="top" align="start" className="w-64">
            <DropdownMenuGroup>
              <DropdownMenuLabel>Thinking</DropdownMenuLabel>
              <DropdownMenuRadioGroup
                value={thinking.id}
                onValueChange={(value) => setThinkingLevel(String(value))}
              >
                {thinkingLevels.map((level) => (
                  <DropdownMenuRadioItem
                    closeOnClick
                    key={level.id}
                    value={level.id}
                  >
                    <OptionText
                      label={level.label}
                      description={level.description}
                    />
                  </DropdownMenuRadioItem>
                ))}
              </DropdownMenuRadioGroup>
            </DropdownMenuGroup>
          </DropdownMenuContent>
        </DropdownMenu>
      )}
    </div>
  )
}

/** The meter's colour at each phase: quiet until compaction is close. */
const contextTone = {
  clear: "text-muted-foreground",
  near: "text-warning-foreground",
  compacting: "text-warning-foreground",
  full: "text-danger-foreground",
} as const

const contextFill = {
  clear: "bg-foreground/55",
  near: "bg-warning-foreground",
  compacting: "bg-warning-foreground",
  full: "bg-danger-foreground",
} as const

/** A small ring filled as far as the conversation fills the window. */
function ContextRing({ view }: { view: ContextView }) {
  const radius = 6
  const circumference = 2 * Math.PI * radius
  const filled = Math.min(1, view.usedShare) * circumference
  return (
    <svg
      viewBox="0 0 16 16"
      aria-hidden
      className={cn("size-3.5 shrink-0 -rotate-90", contextTone[view.phase])}
    >
      <circle
        cx="8"
        cy="8"
        r={radius}
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
        className="opacity-25"
      />
      <circle
        cx="8"
        cy="8"
        r={radius}
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        strokeDasharray={`${filled} ${circumference}`}
      />
    </svg>
  )
}

/** Used, and where it's compacted, along what a request may hold. */
function ContextBar({ view }: { view: ContextView }) {
  return (
    <div
      role="meter"
      aria-label="Context used"
      aria-valuemin={0}
      aria-valuemax={view.limit}
      aria-valuenow={Math.min(view.used, view.limit)}
      aria-valuetext={`${fullTokens(view.used)} of ${fullTokens(view.limit)} tokens`}
      className="relative h-1.5 w-full rounded-full bg-muted"
    >
      <div
        className={cn("h-full rounded-full", contextFill[view.phase])}
        style={{ width: `${Math.min(100, view.usedShare * 100)}%` }}
      />
      {view.thresholdShare !== undefined && (
        <div
          aria-hidden
          title="Compaction"
          className="absolute -top-0.5 h-2.5 w-0.5 -translate-x-1/2 rounded-full bg-foreground/70"
          style={{ left: `${Math.min(100, view.thresholdShare * 100)}%` }}
        />
      )}
    </div>
  )
}

function ContextFact({
  label,
  children,
}: {
  label: string
  children: React.ReactNode
}) {
  return (
    <>
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="min-w-0 text-foreground">{children}</dd>
    </>
  )
}

/** When the next compaction comes, in words. */
function compactionText(view: ContextView) {
  if (view.threshold === undefined) return "Shown once the agent has answered"
  const at = `${fullTokens(view.threshold)} (${percent(view.thresholdShare ?? 0)})`
  if (view.requested) return `Asked for: before the next request`
  if (!view.auto) return `Off: only when asked for · its point is ${at}`
  if (view.untilCompaction !== undefined && view.untilCompaction <= 0) {
    return `At ${at} · due before the next request`
  }
  return `At ${at} · ${compactTokens(view.untilCompaction ?? 0)} to go`
}

const warning = "bg-warning-surface text-warning-foreground"
const danger = "bg-danger-surface text-danger-foreground"

/** What the phase means for the next request, when it's worth saying. */
function phaseNote(view: ContextView) {
  if (view.phase === "full") {
    return view.auto
      ? {
          tone: warning,
          text: "More than the model can read: the agent's earlier work is summarized before its next request.",
        }
      : {
          tone: danger,
          text: "More than the model can read, and automatic compaction is off: the next request may fail.",
        }
  }
  if (view.phase === "compacting") {
    return view.auto
      ? {
          tone: warning,
          text: "Past the compaction point: the agent's earlier work is summarized before its next request.",
        }
      : {
          tone: warning,
          text: "Past the compaction point, and automatic compaction is off: the agent may run out of room.",
        }
  }
  return undefined
}

/**
 * The composer's context meter: how much of its model's context window the
 * conversation holds, what's left, and when it's compacted, from the agent
 * server's `forge:context:<agent>` state (forge_common's
 * `ContextWindowPlugin`) for the agent answering. With the model section,
 * the model chosen there sets the window (the next request runs on it), so
 * it shows before the first message too. Nothing shows until there's a
 * window to measure against.
 */
function AdkContextMeter({ className }: { className?: string }) {
  const state = useAdkSessionState()
  const agent = useAdkAgentInfo()
  const nameOf = useAgentName()
  const { modelSettings, title } = useAssistantSettings()
  const found = currentContext(state, agent?.name)
  const chosen = modelSettings?.models.find(
    (option) => option.id === modelSettings.model
  )
  const view = contextView(found?.context, chosen)
  if (!view) return null

  const agentName = found
    ? nameOf(found.agent)
    : agent?.name
      ? nameOf(agent.name)
      : (title ?? "Assistant")
  const note = phaseNote(view)
  const used = percent(view.usedShare)
  const summary = view.empty
    ? `Context: nothing used yet, ${compactTokens(view.limit)} tokens available`
    : `Context: ${compactTokens(view.used)} of ${compactTokens(view.limit)} tokens used (${used}), ` +
      (view.left >= 0
        ? `${compactTokens(view.left)} left`
        : `${compactTokens(-view.left)} over`) +
      (view.threshold !== undefined
        ? view.auto
          ? `; compacts at ${compactTokens(view.threshold)}`
          : "; automatic compaction is off"
        : "")

  return (
    <Popover>
      <PopoverTrigger
        render={
          <Button
            variant="ghost"
            size="xs"
            className={cn(composerChip, className)}
          />
        }
        aria-label={summary}
        data-slot="adk-context-meter"
        data-phase={view.phase}
      >
        <ContextRing view={view} />
        <span
          className={cn(
            "tabular-nums",
            view.phase === "clear" ? "text-foreground" : contextTone[view.phase]
          )}
        >
          {used}
        </span>
        <span className="@max-[30rem]:hidden">context</span>
      </PopoverTrigger>
      <PopoverContent side="top" align="start" className="w-80 gap-3">
        <div className="flex items-baseline justify-between gap-2">
          <p className="text-sm font-medium text-foreground">Context window</p>
          <p className="truncate text-muted-foreground">{agentName}</p>
        </div>
        <div className="flex flex-col gap-2">
          <p className="flex items-baseline justify-between gap-2">
            <span className="text-foreground tabular-nums">
              <span className="text-sm font-medium">
                {fullTokens(view.used)}
              </span>{" "}
              <span className="text-muted-foreground">
                of {fullTokens(view.limit)} tokens
              </span>
            </span>
            <span
              className={cn(
                "font-medium tabular-nums",
                view.phase === "clear"
                  ? "text-foreground"
                  : contextTone[view.phase]
              )}
            >
              {used}
            </span>
          </p>
          <ContextBar view={view} />
        </div>
        {note && (
          <p className={cn("rounded-(--radius-chip) px-2 py-1.5", note.tone)}>
            {note.text}
          </p>
        )}
        <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1.5">
          <ContextFact label="Left">
            {view.left >= 0
              ? `${fullTokens(view.left)} tokens before the limit`
              : `${fullTokens(-view.left)} tokens over the limit`}
          </ContextFact>
          <ContextFact label="Compaction">{compactionText(view)}</ContextFact>
          {view.model && (
            <ContextFact label="Model">
              {view.model}
              {view.window !== undefined && (
                <span className="text-muted-foreground">
                  {` · ${compactTokens(view.window)} window`}
                  {view.reply
                    ? `, ${compactTokens(view.reply)} kept for replies`
                    : ""}
                </span>
              )}
            </ContextFact>
          )}
          <ContextFact label="Counted">
            {view.empty
              ? "Nothing in the conversation yet"
              : view.estimated
                ? "Estimated, before the agent's last request"
                : "By the model, after its last reply"}
          </ContextFact>
          {view.compactions > 0 && (
            <ContextFact label="Compacted">
              {view.compactions === 1 ? "Once" : `${view.compactions} times`}
              {view.lastCompaction && (
                <span className="text-muted-foreground">
                  {` · last ${compactTokens(view.lastCompaction.tokensBefore)} → ${compactTokens(view.lastCompaction.tokensAfter)}`}
                </span>
              )}
            </ContextFact>
          )}
        </dl>
        {view.modelChanged && (
          <p className="text-muted-foreground">
            Counted on the model that last answered; this is the window of the
            one chosen, which the next request runs on.
          </p>
        )}
      </PopoverContent>
    </Popover>
  )
}

/**
 * The composer's bottom row: the model section (`showModels`) and the
 * context meter (unless `showContext` is false).
 */
function AdkComposerActions() {
  const { showModels, showContext = true } = useAssistantSettings()
  return (
    <>
      {showModels && <AdkModelSection />}
      {showContext && <AdkContextMeter />}
    </>
  )
}

/* -------------------------------------------------------------------------- */
/* Follow-up questions                                                        */
/* -------------------------------------------------------------------------- */

/**
 * Above the composer once a reply is done: the follow-up questions the agent
 * server wrote for it (`suggestFollowUps` on `useAdkAssistant`), as chips
 * that send their question, wrapping onto more lines in a narrow chat so
 * each can be read; while there are none, the runtime's own suggestions.
 * Hidden while the agent works or waits on the person.
 */
function AdkFollowUps() {
  const state = useAdkSessionState()
  const suggestions = React.useMemo(() => followUpsOf(state), [state])
  const running = useAuiState((s) => s.thread.isRunning)
  const waiting = useAdkLongRunningToolIds().length > 0
  if (suggestions.length === 0) return <ThreadFollowupSuggestions />
  if (running || waiting) return null
  return (
    <div
      data-slot="adk-follow-ups"
      aria-label="Suggested follow-ups"
      role="group"
      className="flex animate-in flex-wrap justify-center gap-1.5 duration-300 fade-in slide-in-from-bottom-1 motion-reduce:animate-none"
    >
      {suggestions.map((prompt) => (
        <ThreadPrimitive.Suggestion
          key={prompt}
          prompt={prompt}
          send
          className="max-w-full rounded-(--radius-control) border border-border px-2.5 py-1 text-start text-xs text-pretty transition-colors ease-in hover:border-foreground/25 hover:bg-foreground/[0.03] motion-reduce:transition-none"
        >
          {prompt}
        </ThreadPrimitive.Suggestion>
      ))}
    </div>
  )
}

export {
  AdkAgentIndicator,
  AdkArtifactsMenu,
  AdkComposerActions,
  AdkContextMeter,
  AdkEscalationBanner,
  AdkFollowUps,
  AdkMessageAuthor,
  AdkModelSection,
}
