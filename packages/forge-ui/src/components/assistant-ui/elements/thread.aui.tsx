"use client"

import {
  ComposerAddAttachment,
  ComposerAttachments,
  UserMessageAttachments,
} from "@/components/assistant-ui/elements/attachment.aui"
import { File } from "@/components/assistant-ui/elements/file"
import { ThreadFollowupSuggestions } from "@/components/assistant-ui/elements/follow-up-suggestions.aui"
import { Image } from "@/components/assistant-ui/elements/image"
import { MarkdownText } from "@/components/assistant-ui/elements/markdown-text"
import { AttachmentNotice } from "@/components/assistant-ui/elements/attachment-notice"
import { QuoteToolbar } from "@/components/assistant-ui/elements/quote-toolbar.aui"
import {
  Reasoning,
  ReasoningContent,
  ReasoningRoot,
  ReasoningText,
  ReasoningTrigger,
} from "@/components/assistant-ui/elements/reasoning.aui"
import { ToolFallback } from "@/components/assistant-ui/elements/tool-fallback.aui"
import { useReasoningDuration } from "@/components/assistant-ui/elements/use-reasoning-duration"
import {
  ToolGroupContent,
  ToolGroupRoot,
  ToolGroupTrigger,
} from "@/components/assistant-ui/elements/tool-group.aui"
import { TooltipIconButton } from "@/components/assistant-ui/elements/tooltip-icon-button"
import { useFollowReply } from "@/components/assistant-ui/elements/use-follow-reply"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import { cn } from "cn"
import { labelledFileName } from "@/lib/attachments"
import {
  ActionBarMorePrimitive,
  ActionBarPrimitive,
  AuiIf,
  type AssistantState,
  BranchPickerPrimitive,
  ComposerPrimitive,
  ErrorPrimitive,
  groupPartByType,
  MessagePrimitive,
  SuggestionPrimitive,
  ThreadPrimitive,
  type FileMessagePartComponent,
  type ImageMessagePartComponent,
  type TextMessagePartComponent,
  type ToolCallMessagePartComponent,
  unstable_useComposerInputHistory,
  useAuiState,
} from "@assistant-ui/react"
import {
  ArrowDownIcon,
  ArrowUpIcon,
  AudioLinesIcon,
  CheckIcon,
  ChevronLeftIcon,
  ChevronRightIcon,
  CopyIcon,
  DownloadIcon,
  MicIcon,
  MoreHorizontalIcon,
  PencilIcon,
  PhoneIcon,
  RefreshCwIcon,
  SquareIcon,
  ThumbsDownIcon,
  ThumbsUpIcon,
} from "@/components/assistant-ui/elements/aui-icons"
import {
  createContext,
  useContext,
  type ComponentType,
  type FC,
  type PropsWithChildren,
  useRef,
  useState,
} from "react"

export type ThreadGroupPart = MessagePrimitive.GroupedParts.GroupPart

/**
 * Optional component overrides for the thread. `AssistantMessage` and
 * `Welcome` replace whole sections; the remaining slots override how the
 * assistant message renders tool calls and part groups. Tool UIs registered
 * by name (toolkit `render`, `useAssistantDataUI`) take precedence over
 * `ToolFallback`. When `TaskGroup` is set, tool calls that carry a nested
 * conversation and have no registered UI render through it instead of the
 * tool group; without it they render like any other tool call.
 */
export type ThreadComponents = {
  AssistantMessage?: ComponentType | undefined
  Welcome?: ComponentType | undefined
  ToolFallback?: ToolCallMessagePartComponent | undefined
  ToolGroup?:
    ComponentType<PropsWithChildren<{ group: ThreadGroupPart }>> | undefined
  ReasoningGroup?:
    ComponentType<PropsWithChildren<{ group: ThreadGroupPart }>> | undefined
  TaskGroup?: ComponentType<{ group: ThreadGroupPart }> | undefined
  /** Rendered above each assistant message's content, e.g. its author. */
  MessageHeader?: ComponentType | undefined
  /** Draws an assistant message's text parts; default `MarkdownText`. */
  Text?: ComponentType | undefined
  /** Under an assistant message's content, above its actions. */
  MessageFooter?: ComponentType | undefined
  /** Beside an assistant message's actions, e.g. its timing and tokens. */
  MessageMeta?: ComponentType | undefined
  /**
   * Above the composer once a reply is done, in place of the runtime's own
   * follow-up suggestions, e.g. ones a server writes.
   */
  FollowUps?: ComponentType | undefined
  /** Rendered in the composer after the attachment button, e.g. a model picker. */
  ComposerActions?: ComponentType | undefined
  /** Right-aligned controls immediately before Send. */
  ComposerTrailingActions?: ComponentType | undefined
  /**
   * Rendered at the top of the composer, before attachments, e.g. what the
   * agent will see with the message.
   */
  ComposerHeader?: ComponentType | undefined
  /** Outside the input surface: persistent task progress. */
  ComposerLead?: ComponentType | undefined
  /**
   * Trigger popovers for the composer (`ComposerTriggerPopover` for `/`
   * commands or `@` mentions), rendered inside its trigger root.
   */
  ComposerTriggers?: ComponentType | undefined
}

const messageGroupBy = groupPartByType({
  reasoning: ["group-chainOfThought", "group-reasoning"],
  "tool-call": ["group-chainOfThought", "group-tool"],
  "standalone-tool-call": [],
})

type ThreadGroupKey =
  "group-chainOfThought" | "group-reasoning" | "group-tool" | "group-task"

const TASK_GROUP_PATH: readonly ThreadGroupKey[] = [
  "group-chainOfThought",
  "group-task",
]

const taskAwareGroupBy = (
  part: Parameters<typeof messageGroupBy>[0],
  context?: Parameters<typeof messageGroupBy>[1]
): readonly ThreadGroupKey[] => {
  const path = messageGroupBy(part, context)
  return part.type === "tool-call" &&
    part.messages !== undefined &&
    path.length > 0 &&
    !context?.toolUIs?.[part.toolName]?.length
    ? TASK_GROUP_PATH
    : path
}

/**
 * Tool calls with their own registered UI (approvals, sign-in, generative UI)
 * render in the message flow; only fallback calls fold into the tool group.
 */
const withStandaloneToolUIs =
  (groupBy: typeof taskAwareGroupBy): typeof taskAwareGroupBy =>
  (part, context) =>
    part.type === "tool-call" && context?.toolUIs?.[part.toolName]?.length
      ? []
      : groupBy(part, context)

const defaultGroupBy = withStandaloneToolUIs(messageGroupBy)
const taskGroupBy = withStandaloneToolUIs(taskAwareGroupBy)

export type ThreadProps = {
  components?: ThreadComponents | undefined
  autoFocus?: boolean | undefined
  /** The composer's placeholder. Default "Send a message...". */
  placeholder?: string | undefined
}

const EMPTY_COMPONENTS: ThreadComponents = {}

const ThreadComponentsContext =
  createContext<ThreadComponents>(EMPTY_COMPONENTS)

// Startup exposes a loading placeholder thread; treat it as a new chat so
// the composer mounts centered. Loads after startup keep the docked layout.
const isNewChatView = (s: AssistantState) =>
  s.thread.messages.length === 0 && (!s.thread.isLoading || s.threads.isLoading)

// A switched thread that is still fetching its history: skeleton, not welcome.
const isHistoryLoadingView = (s: AssistantState) =>
  s.thread.messages.length === 0 &&
  s.thread.isLoading &&
  !s.thread.isDisabled &&
  !s.threads.isLoading

const ThreadHistorySkeleton: FC = () => (
  <div
    data-slot="aui_thread-history-skeleton"
    role="status"
    className="flex animate-in flex-col gap-y-6 fill-mode-both [animation-delay:150ms] [animation-duration:200ms] fade-in"
  >
    <span className="sr-only">Loading conversation</span>
    <Skeleton className="ml-auto h-9 w-2/5 rounded-(--radius-band) motion-reduce:animate-none" />
    <div className="flex flex-col gap-y-2">
      <Skeleton className="h-4 w-11/12 motion-reduce:animate-none" />
      <Skeleton className="h-4 w-4/5 motion-reduce:animate-none" />
      <Skeleton className="h-4 w-3/5 motion-reduce:animate-none" />
    </div>
    <Skeleton className="ml-auto h-9 w-1/3 rounded-(--radius-band) motion-reduce:animate-none" />
    <div className="flex flex-col gap-y-2">
      <Skeleton className="h-4 w-10/12 motion-reduce:animate-none" />
      <Skeleton className="h-4 w-2/3 motion-reduce:animate-none" />
    </div>
  </div>
)

export const Thread: FC<ThreadProps> = ({
  components = EMPTY_COMPONENTS,
  autoFocus = true,
  placeholder = "Send a message...",
}) => {
  const isEmpty = useAuiState(isNewChatView)

  return (
    <ThreadComponentsContext.Provider value={components}>
      <ThreadRoot
        isEmpty={isEmpty}
        autoFocus={autoFocus}
        placeholder={placeholder}
      />
    </ThreadComponentsContext.Provider>
  )
}

const ThreadRoot: FC<{
  isEmpty: boolean
  autoFocus: boolean
  placeholder: string
}> = ({ isEmpty, autoFocus, placeholder }) => {
  const {
    Welcome = ThreadWelcome,
    FollowUps,
    ComposerLead,
  } = useContext(ThreadComponentsContext)
  // A new message scrolls to the top (turnAnchor="top") and its reply fills
  // the space below; once the reply outgrows it, the thread follows it.
  const viewportRef = useRef<HTMLDivElement>(null)
  useFollowReply(viewportRef)

  return (
    <ThreadPrimitive.Root
      className="aui-root aui-thread-root @container flex h-full flex-col bg-background text-sm"
      style={{
        ["--thread-max-width" as string]: "44rem",
        ["--composer-bg" as string]:
          "color-mix(in oklab, var(--color-muted) 30%, transparent)",
        ["--composer-radius" as string]: "1rem",
        ["--composer-padding" as string]: "8px",
      }}
    >
      <ThreadPrimitive.Viewport
        ref={viewportRef}
        turnAnchor="top"
        data-slot="aui_thread-viewport"
        className="relative flex flex-1 flex-col overflow-x-auto overflow-y-scroll scroll-smooth"
      >
        <div
          className={cn(
            "mx-auto flex w-full max-w-(--thread-max-width) flex-1 flex-col px-4 pt-4",
            isEmpty && "justify-center"
          )}
        >
          <AuiIf condition={isNewChatView}>
            <Welcome />
          </AuiIf>
          <AuiIf condition={isHistoryLoadingView}>
            <ThreadHistorySkeleton />
          </AuiIf>

          <div
            data-slot="aui_message-group"
            className="mb-14 flex flex-col gap-y-6 empty:hidden"
          >
            <ThreadPrimitive.Messages>
              {() => <ThreadMessage />}
            </ThreadPrimitive.Messages>
          </div>

          <ThreadPrimitive.ViewportFooter
            className={cn(
              "aui-thread-viewport-footer flex flex-col gap-4 overflow-visible bg-background pb-4 md:pb-6",
              !isEmpty &&
                "sticky bottom-0 mt-auto rounded-t-(--composer-radius)"
            )}
          >
            <ThreadScrollToBottom />
            {FollowUps ? <FollowUps /> : <ThreadFollowupSuggestions />}
            <div className="flex flex-col gap-2">
              {ComposerLead && <ComposerLead />}
              <Composer autoFocus={autoFocus} placeholder={placeholder} />
            </div>
            <AuiIf condition={(s) => isNewChatView(s) && s.composer.isEmpty}>
              <ThreadSuggestions />
            </AuiIf>
          </ThreadPrimitive.ViewportFooter>
        </div>
      </ThreadPrimitive.Viewport>
      <QuoteToolbar />
    </ThreadPrimitive.Root>
  )
}

const ThreadMessage: FC = () => {
  const { AssistantMessage: AssistantMessageComponent = AssistantMessage } =
    useContext(ThreadComponentsContext)
  const role = useAuiState((s) => s.message.role)
  const isEditing = useAuiState((s) => s.message.composer.isEditing)
  const isSpoken = useAuiState((s) => s.message.metadata.modality === "voice")

  if (isEditing) return <EditComposer />
  if (isSpoken) return <SpokenMessage />
  if (role === "user") return <UserMessage />
  return <AssistantMessageComponent />
}

type VoiceRunPosition = "single" | "start" | "middle" | "end"

const useVoiceRunPosition = (): VoiceRunPosition =>
  useAuiState((s) => {
    const before =
      s.thread.messages[s.message.index - 1]?.metadata.modality === "voice"
    const after =
      s.thread.messages[s.message.index + 1]?.metadata.modality === "voice"
    if (before) return after ? "middle" : "end"
    return after ? "start" : "single"
  })

const SpokenText: TextMessagePartComponent = ({ text }) => (
  <p className="aui-spoken-message-text m-0">{text}</p>
)

const SpokenMessage: FC = () => {
  const role = useAuiState((s) => s.message.role)
  const position = useVoiceRunPosition()
  const isSpeaking = useAuiState(
    (s) =>
      s.message.role === "assistant" && s.message.status?.type === "running"
  )
  const opensExchange = position === "start" || position === "single"

  return (
    <MessagePrimitive.Root
      data-slot="aui_spoken-message-root"
      data-role={role}
      data-voice-run={position}
      className={cn(
        "aui-spoken-message mx-2 bg-muted/40 px-3 py-1.5 [contain-intrinsic-size:auto_48px] [content-visibility:auto]",
        position === "single" && "rounded-xl py-2",
        position === "start" && "rounded-t-xl pt-2",
        position === "middle" && "-mt-6",
        position === "end" && "-mt-6 rounded-b-xl pb-2"
      )}
    >
      {opensExchange && (
        <div
          data-slot="aui_spoken-exchange-header"
          className="mb-1.5 flex items-center gap-1.5 text-xs text-muted-foreground"
        >
          <PhoneIcon className="size-3" aria-hidden />
          <span>Voice conversation</span>
        </div>
      )}
      <div
        data-slot="aui_spoken-message-content"
        className="flex items-start gap-2 text-sm leading-relaxed text-foreground"
      >
        <span className="mt-1 shrink-0 text-muted-foreground" aria-hidden>
          {role === "user" ? (
            <MicIcon className="size-3.5" />
          ) : (
            <AudioLinesIcon className="size-3.5" />
          )}
        </span>
        <span className="sr-only">
          {role === "user" ? "You said" : "Assistant said"}
        </span>
        <div className="min-w-0 flex-1 wrap-break-word">
          <MessagePrimitive.Parts components={{ Text: SpokenText }} />
          {isSpeaking && (
            <span
              data-slot="aui_spoken-message-indicator"
              role="status"
              className="ms-1 animate-pulse font-sans text-muted-foreground"
              aria-label="Assistant is speaking"
            >
              ●
            </span>
          )}
        </div>
        <SpokenActionBar />
      </div>
    </MessagePrimitive.Root>
  )
}

const SpokenActionBar: FC = () => {
  return (
    <ActionBarPrimitive.Root
      hideWhenRunning
      autohide="always"
      className="aui-spoken-action-bar flex shrink-0 gap-1 text-muted-foreground"
    >
      <ActionBarPrimitive.Copy
        render={<TooltipIconButton tooltip="Copy" className="size-6" />}
      >
        <AuiIf condition={(s) => s.message.isCopied}>
          <CheckIcon className="animate-in duration-200 ease-out zoom-in-50 fade-in" />
        </AuiIf>
        <AuiIf condition={(s) => !s.message.isCopied}>
          <CopyIcon className="animate-in duration-150 zoom-in-75 fade-in" />
        </AuiIf>
      </ActionBarPrimitive.Copy>
    </ActionBarPrimitive.Root>
  )
}

const ThreadScrollToBottom: FC = () => {
  return (
    <ThreadPrimitive.ScrollToBottom
      render={
        <TooltipIconButton
          tooltip="Scroll to bottom"
          variant="outline"
          className="aui-thread-scroll-to-bottom absolute -top-12 z-10 self-center rounded-full p-4 disabled:invisible dark:border-border dark:bg-background dark:hover:bg-accent"
        />
      }
    >
      <ArrowDownIcon />
    </ThreadPrimitive.ScrollToBottom>
  )
}

const ThreadWelcome: FC = () => {
  return (
    <div className="aui-thread-welcome-root mb-6 flex flex-col px-2">
      <p className="aui-thread-welcome-message-inner animate-in text-2xl font-medium tracking-tight duration-200 fill-mode-both fade-in slide-in-from-bottom-1">
        How can I help you today?
      </p>
    </div>
  )
}

const ThreadSuggestions: FC = () => {
  return (
    <div className="aui-thread-welcome-suggestions flex w-full flex-col">
      <ThreadPrimitive.Suggestions>
        {() => <ThreadSuggestionItem />}
      </ThreadPrimitive.Suggestions>
    </div>
  )
}

const ThreadSuggestionItem: FC = () => {
  return (
    <div className="aui-thread-welcome-suggestion-display animate-in duration-200 fill-mode-both fade-in slide-in-from-bottom-2">
      <SuggestionPrimitive.Trigger
        send
        render={
          <button
            type="button"
            className="aui-thread-welcome-suggestion group flex w-full items-baseline gap-2.5 rounded-md px-2 py-2 text-start text-sm transition-colors outline-none hover:bg-foreground/[0.03] focus-visible:ring-1 focus-visible:ring-ring/50 motion-reduce:transition-none"
          />
        }
      >
        <span
          aria-hidden
          className="font-mono text-xs text-muted-foreground/60 transition-colors group-hover:text-foreground motion-reduce:transition-none"
        >
          {">"}
        </span>
        <span className="min-w-0 flex-1 truncate">
          <SuggestionPrimitive.Title className="aui-thread-welcome-suggestion-text-1 text-foreground" />{" "}
          <SuggestionPrimitive.Description className="aui-thread-welcome-suggestion-text-2 text-muted-foreground empty:hidden" />
        </span>
      </SuggestionPrimitive.Trigger>
    </div>
  )
}

const Composer: FC<{ autoFocus: boolean; placeholder: string }> = ({
  autoFocus,
  placeholder,
}) => {
  const { ComposerTriggers, ComposerHeader } = useContext(
    ThreadComponentsContext
  )
  const isListening = useAuiState((s) => s.composer.dictation != null)
  const inputHistory = unstable_useComposerInputHistory()
  return (
    <ComposerPrimitive.Unstable_TriggerPopoverRoot>
      <ComposerPrimitive.Root className="aui-composer-root relative flex w-full flex-col">
        {ComposerTriggers && <ComposerTriggers />}
        <ComposerPrimitive.AttachmentDropzone
          render={
            <div
              data-slot="aui_composer-shell"
              className="flex w-full cursor-text flex-col gap-2 rounded-(--composer-radius) border border-foreground/10 bg-(--composer-bg) p-(--composer-padding) transition-[border-color] focus-within:border-foreground/25 data-[dragging=true]:border-dashed data-[dragging=true]:border-ring data-[dragging=true]:bg-[color-mix(in_oklab,var(--color-accent)_50%,var(--color-background))]"
            />
          }
        >
          {ComposerHeader && <ComposerHeader />}
          <AttachmentNotice />
          <ComposerAttachments />
          <ComposerPrimitive.Input
            // ↑ in an empty composer recalls earlier messages, ↓ goes back.
            {...inputHistory}
            placeholder={isListening ? "Listening…" : placeholder}
            className="aui-composer-input max-h-48 min-h-10 w-full resize-none bg-transparent px-2.5 py-1 text-base leading-6 caret-primary outline-none placeholder:text-muted-foreground/60"
            rows={1}
            autoFocus={autoFocus}
            enterKeyHint="send"
            aria-label="Message input"
          />
          <ComposerAction />
        </ComposerPrimitive.AttachmentDropzone>
      </ComposerPrimitive.Root>
    </ComposerPrimitive.Unstable_TriggerPopoverRoot>
  )
}

/**
 * Voice input through the runtime's dictation adapter (e.g. the browser's
 * speech recognition). The composer writes the words into the input as
 * they're recognised; hidden when the runtime has no adapter.
 */
const ComposerDictation: FC = () => (
  <AuiIf condition={(s) => s.thread.capabilities.dictation}>
    <AuiIf condition={(s) => s.composer.dictation == null}>
      <ComposerPrimitive.Dictate
        render={
          <TooltipIconButton
            tooltip="Voice input"
            side="bottom"
            type="button"
            variant="ghost"
            size="icon"
            className="aui-composer-dictate size-7 rounded-full text-muted-foreground hover:bg-muted-foreground/15 hover:text-foreground active:scale-[0.96] motion-reduce:transition-none dark:hover:bg-muted-foreground/30"
            aria-label="Start voice input"
          />
        }
      >
        <MicIcon className="aui-composer-dictate-icon size-4" />
      </ComposerPrimitive.Dictate>
    </AuiIf>
    <AuiIf condition={(s) => s.composer.dictation != null}>
      <ComposerPrimitive.StopDictation
        render={
          <TooltipIconButton
            tooltip="Stop voice input"
            side="bottom"
            type="button"
            variant="ghost"
            size="icon"
            className="aui-composer-stop-dictation size-7 rounded-full bg-destructive/10 text-destructive hover:bg-destructive/15 hover:text-destructive"
            aria-label="Stop voice input"
          />
        }
      >
        <SquareIcon className="aui-composer-stop-dictation-icon size-3 animate-pulse fill-current" />
      </ComposerPrimitive.StopDictation>
    </AuiIf>
  </AuiIf>
)

const ComposerAction: FC = () => {
  const { ComposerActions, ComposerTrailingActions } = useContext(
    ThreadComponentsContext
  )
  // The stop control only cancels the send while no run it could stop is going.
  const isSending = useAuiState(
    (s) =>
      s.composer.submission !== undefined &&
      !(s.thread.isRunning && s.thread.capabilities.cancel)
  )

  return (
    <div className="aui-composer-action-wrapper relative flex items-center justify-between">
      {/* Its controls keep their size; on a very narrow composer the row
          scrolls rather than letting them overlap. */}
      <div className="flex min-w-0 [scrollbar-width:none] items-center gap-1 overflow-x-auto [&::-webkit-scrollbar]:hidden">
        <ComposerAddAttachment />
        <ComposerDictation />
        {ComposerActions && <ComposerActions />}
      </div>
      <div className="ml-auto flex shrink-0 items-center gap-1.5 pl-1.5">
        {ComposerTrailingActions && <ComposerTrailingActions />}
        <AuiIf
          condition={(s) =>
            !s.composer.canCancel ||
            (s.thread.voice !== undefined &&
              s.composer.submission === undefined)
          }
        >
          <ComposerPrimitive.Send
            render={
              <TooltipIconButton
                tooltip="Send message"
                side="bottom"
                type="button"
                variant="default"
                size="icon"
                className="aui-composer-send size-7 rounded-full"
                aria-label="Send message"
              />
            }
          >
            <ArrowUpIcon className="aui-composer-send-icon size-4" />
          </ComposerPrimitive.Send>
        </AuiIf>
        <AuiIf
          condition={(s) =>
            s.composer.canCancel &&
            (s.thread.voice === undefined ||
              s.composer.submission !== undefined)
          }
        >
          <ComposerPrimitive.Cancel
            render={
              <Button
                type="button"
                variant="default"
                size="icon"
                className="aui-composer-cancel size-7 rounded-full"
                aria-label={isSending ? "Cancel sending" : "Stop generating"}
              />
            }
          >
            <SquareIcon className="aui-composer-cancel-icon size-3.5 fill-current" />
          </ComposerPrimitive.Cancel>
        </AuiIf>
      </div>
    </div>
  )
}

const MessageError: FC = () => {
  return (
    <MessagePrimitive.Error>
      <ErrorPrimitive.Root className="aui-message-error-root mt-2 rounded-(--radius-card) border border-danger-border bg-danger-surface p-3 text-xs text-danger-foreground">
        <ErrorPrimitive.Message className="aui-message-error-message line-clamp-2" />
      </ErrorPrimitive.Root>
    </MessagePrimitive.Error>
  )
}

const AssistantMessage: FC = () => {
  const {
    ToolFallback: ToolFallbackComponent = ToolFallback,
    ToolGroup,
    ReasoningGroup,
    TaskGroup: TaskGroupComponent,
    MessageHeader,
    Text: TextComponent = MarkdownText,
    MessageFooter,
    MessageMeta,
  } = useContext(ThreadComponentsContext)
  const groupBy = TaskGroupComponent ? taskGroupBy : defaultGroupBy

  const ACTION_BAR_PT = "pt-1.5"
  // Keep the action bar inside the contained root's paint box, then cancel its reserved space in flow.
  const ACTION_BAR_HEIGHT = `min-h-7.5 ${ACTION_BAR_PT}`

  return (
    <MessagePrimitive.Root
      data-slot="aui_assistant-message-root"
      data-role="assistant"
      className="relative -mb-7.5 animate-in pb-7.5 duration-150 [contain-intrinsic-size:auto_200px] [content-visibility:auto] fade-in slide-in-from-bottom-1"
    >
      <div
        data-slot="aui_assistant-message-content"
        className={cn(
          "px-2 leading-relaxed wrap-break-word text-foreground",
          // Tool steps sit a little apart from each other; text is set well
          // apart from the steps around it.
          "[&>*+*]:mt-1 [&>*+.aui-md]:mt-3 [&>.aui-md+*]:mt-3"
        )}
      >
        {MessageHeader && <MessageHeader />}
        <MessagePrimitive.GroupedParts groupBy={groupBy}>
          {({ part, children }) => {
            switch (part.type) {
              case "group-chainOfThought":
                return <div data-slot="aui_chain-of-thought">{children}</div>
              case "group-task":
                return TaskGroupComponent ? (
                  <TaskGroupComponent group={part} />
                ) : null
              case "group-tool":
                if (ToolGroup) {
                  return <ToolGroup group={part}>{children}</ToolGroup>
                }
                return (
                  <DefaultToolGroup group={part}>{children}</DefaultToolGroup>
                )
              case "group-reasoning": {
                if (ReasoningGroup) {
                  return (
                    <ReasoningGroup group={part}>{children}</ReasoningGroup>
                  )
                }
                return (
                  <DefaultReasoningGroup group={part}>
                    {children}
                  </DefaultReasoningGroup>
                )
              }
              case "text":
                return <TextComponent />
              case "reasoning":
                return <Reasoning {...part} />
              case "tool-call":
                return part.toolUI ?? <ToolFallbackComponent {...part} />
              case "data":
                return part.dataRendererUI
              case "file":
                return (
                  <div data-slot="aui_assistant-message-file" className="py-1">
                    <File {...part} />
                  </div>
                )
              case "image":
                return (
                  <div data-slot="aui_assistant-message-image" className="py-1">
                    <Image {...part} />
                  </div>
                )
              // Nothing written yet, or it ended on a tool call: say it's
              // still working, in the reasoning label's shimmer.
              case "indicator":
                return (
                  <span
                    data-slot="aui_assistant-message-indicator"
                    role="status"
                    className="inline-flex items-center gap-2 py-1 font-sans text-sm leading-none text-muted-foreground"
                  >
                    <span
                      className="size-1.5 animate-pulse rounded-full bg-current motion-reduce:animate-none"
                      aria-hidden
                    />
                    Working…
                  </span>
                )
              default:
                return null
            }
          }}
        </MessagePrimitive.GroupedParts>
        <MessageError />
        {MessageFooter && <MessageFooter />}
      </div>

      <div
        data-slot="aui_assistant-message-footer"
        className={cn(
          "ms-2 flex flex-wrap items-center gap-y-1",
          ACTION_BAR_HEIGHT
        )}
      >
        <BranchPicker />
        <AssistantActionBar />
        {MessageMeta && <MessageMeta />}
      </div>
    </MessagePrimitive.Root>
  )
}

/**
 * Collapsed by default, but opens while a call inside waits on the user
 * (an approval, sign-in or question), as ToolFallback does for one call.
 */
const DefaultToolGroup: FC<PropsWithChildren<{ group: ThreadGroupPart }>> = ({
  group,
  children,
}) => {
  const requiresAction = group.status.type === "requires-action"
  const [open, setOpen] = useState(requiresAction)
  const [prevRequiresAction, setPrevRequiresAction] = useState(requiresAction)
  if (requiresAction !== prevRequiresAction) {
    setPrevRequiresAction(requiresAction)
    if (requiresAction) setOpen(true)
  }
  return (
    <ToolGroupRoot variant="ghost" open={open} onOpenChange={setOpen}>
      <ToolGroupTrigger
        count={group.indices.length}
        active={group.status.type === "running"}
      />
      <ToolGroupContent>{children}</ToolGroupContent>
    </ToolGroupRoot>
  )
}

/**
 * The model's thoughts: "Thinking" while they stream, then "Thought for 12
 * seconds", collapsed.
 */
const DefaultReasoningGroup: FC<
  PropsWithChildren<{ group: ThreadGroupPart }>
> = ({ group, children }) => {
  const running = group.status.type === "running"
  const duration = useReasoningDuration(group.indices[0] ?? 0, running)
  return (
    <ReasoningRoot streaming={running}>
      <ReasoningTrigger active={running} duration={duration} />
      <ReasoningContent aria-busy={running}>
        <ReasoningText>{children}</ReasoningText>
      </ReasoningContent>
    </ReasoningRoot>
  )
}

const AssistantActionBar: FC = () => {
  return (
    <ActionBarPrimitive.Root
      hideWhenRunning
      autohide="not-last"
      className="aui-assistant-action-bar-root col-start-3 row-start-2 -ms-1 flex animate-in gap-1 text-muted-foreground duration-200 fade-in"
    >
      <ActionBarPrimitive.Copy render={<TooltipIconButton tooltip="Copy" />}>
        <AuiIf condition={(s) => s.message.isCopied}>
          <CheckIcon className="animate-in duration-200 ease-out zoom-in-50 fade-in" />
        </AuiIf>
        <AuiIf condition={(s) => !s.message.isCopied}>
          <CopyIcon className="animate-in duration-150 zoom-in-75 fade-in" />
        </AuiIf>
      </ActionBarPrimitive.Copy>
      <AuiIf condition={(s) => s.thread.capabilities.feedback}>
        <ActionBarPrimitive.FeedbackPositive
          render={
            <TooltipIconButton
              tooltip="Helpful"
              className="data-[submitted=true]:bg-accent data-[submitted=true]:text-accent-foreground"
            />
          }
        >
          <ThumbsUpIcon />
        </ActionBarPrimitive.FeedbackPositive>
        <ActionBarPrimitive.FeedbackNegative
          render={
            <TooltipIconButton
              tooltip="Not helpful"
              className="data-[submitted=true]:bg-accent data-[submitted=true]:text-accent-foreground"
            />
          }
        >
          <ThumbsDownIcon />
        </ActionBarPrimitive.FeedbackNegative>
      </AuiIf>
      <ActionBarPrimitive.Reload
        render={<TooltipIconButton tooltip="Refresh" />}
      >
        <RefreshCwIcon />
      </ActionBarPrimitive.Reload>
      <ActionBarMorePrimitive.Root>
        <ActionBarMorePrimitive.Trigger
          render={
            <TooltipIconButton
              tooltip="More"
              className="data-[state=open]:bg-accent"
            />
          }
        >
          <MoreHorizontalIcon />
        </ActionBarMorePrimitive.Trigger>
        <ActionBarMorePrimitive.Content
          side="bottom"
          align="start"
          sideOffset={6}
          className="aui-action-bar-more-content z-50 min-w-[180px] overflow-hidden rounded-(--radius-band) border border-border bg-popover p-1 text-popover-foreground shadow-lg data-[side=bottom]:slide-in-from-top-2 data-[side=left]:slide-in-from-right-2 data-[side=right]:slide-in-from-left-2 data-[side=top]:slide-in-from-bottom-2 data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=closed]:zoom-out-95 data-[state=open]:animate-in data-[state=open]:fade-in-0 data-[state=open]:zoom-in-95"
        >
          <ActionBarPrimitive.ExportMarkdown
            render={
              <ActionBarMorePrimitive.Item className="aui-action-bar-more-item flex min-h-8 cursor-default items-center gap-2 rounded-(--radius-item) px-2 py-1.5 text-xs outline-none select-none focus:bg-accent focus:text-accent-foreground" />
            }
          >
            <DownloadIcon className="size-4" />
            Export as Markdown
          </ActionBarPrimitive.ExportMarkdown>
        </ActionBarMorePrimitive.Content>
      </ActionBarMorePrimitive.Root>
    </ActionBarPrimitive.Root>
  )
}

/**
 * Your message's text, with a quote it opens with (`> …` lines, as the
 * Quote button writes it) set apart as a quote block above the rest.
 */
const UserText: TextMessagePartComponent = ({ text }) => {
  // A file's "📎 name" part: its chip shows the name instead.
  const labelsFile = useAuiState(
    (s) =>
      labelledFileName(text) !== undefined &&
      s.message.content.some((part) => part.type === "file")
  )
  if (labelsFile) return null
  const lines = text.split("\n")
  const quoted = lines.findIndex((line) => !line.startsWith(">"))
  const quoteEnd = quoted === -1 ? lines.length : quoted
  if (quoteEnd === 0) return <p className="whitespace-pre-line">{text}</p>
  const quote = lines
    .slice(0, quoteEnd)
    .map((line) => line.replace(/^> ?/, ""))
    .join("\n")
  const rest = lines.slice(quoteEnd).join("\n").trim()
  return (
    <>
      <blockquote
        data-slot="aui_user-message-quote"
        className="mb-1.5 line-clamp-4 border-s-2 border-foreground/25 ps-2.5 text-sm whitespace-pre-line text-muted-foreground"
      >
        {quote}
      </blockquote>
      {rest && <p className="whitespace-pre-line">{rest}</p>}
    </>
  )
}

/**
 * A file on your message, named by the "📎 name" part sent just before it
 * (the runtime drops file names; see lib/attachments).
 */
const UserFilePart: FileMessagePartComponent = (part) => {
  const labelled = useAuiState((s) => {
    const content = s.message.content
    const index = content.findIndex(
      (each) => each.type === "file" && each.data === part.data
    )
    const before = content[index - 1]
    return before?.type === "text" ? labelledFileName(before.text) : undefined
  })
  return (
    <div data-slot="aui_user-message-file" className="py-1">
      <File {...part} filename={part.filename ?? labelled} />
    </div>
  )
}

const UserImagePart: ImageMessagePartComponent = (part) => (
  <div data-slot="aui_user-message-image" className="py-1">
    <Image {...part} />
  </div>
)

const UserMessage: FC = () => {
  // A new message scrolls to the top of the viewport (turnAnchor="top"),
  // aligned to this root's top edge. -mt-4 pt-4 leaves room above the bubble
  // there, as for the first message. Every later message is set apart from
  // the reply before it (a margin, outside that edge), so turns read apart.
  return (
    <MessagePrimitive.Root
      data-slot="aui_user-message-root"
      className="-mt-4 grid animate-in auto-rows-auto grid-cols-[minmax(72px,1fr)_auto] content-start gap-y-2 px-2 pt-4 duration-150 [contain-intrinsic-size:auto_200px] [content-visibility:auto] fade-in slide-in-from-bottom-1 not-first:mt-6 [&:where(>*)]:col-start-2"
      data-role="user"
    >
      <UserMessageAttachments />

      <div className="aui-user-message-content-wrapper relative col-start-2 min-w-0">
        <div className="aui-user-message-content peer rounded-(--composer-radius) bg-muted px-4 py-2 wrap-break-word text-foreground empty:hidden">
          <MessagePrimitive.Parts
            components={{
              File: UserFilePart,
              Image: UserImagePart,
              Text: UserText,
            }}
          />
        </div>
        <div className="aui-user-action-bar-wrapper absolute start-0 top-1/2 -translate-x-full -translate-y-1/2 pe-2 peer-empty:hidden rtl:translate-x-full">
          <UserActionBar />
        </div>
      </div>

      <BranchPicker
        data-slot="aui_user-branch-picker"
        className="col-span-full col-start-1 -me-1 justify-end"
      />
    </MessagePrimitive.Root>
  )
}

const UserActionBar: FC = () => {
  return (
    <ActionBarPrimitive.Root
      hideWhenRunning
      autohide="not-last"
      className="aui-user-action-bar-root flex flex-col items-end"
    >
      <ActionBarPrimitive.Edit
        render={
          <TooltipIconButton tooltip="Edit" className="aui-user-action-edit" />
        }
      >
        <PencilIcon />
      </ActionBarPrimitive.Edit>
    </ActionBarPrimitive.Root>
  )
}

const EditComposer: FC = () => {
  return (
    <MessagePrimitive.Root
      data-slot="aui_edit-composer-wrapper"
      className="flex flex-col px-2 [contain-intrinsic-size:auto_200px] [content-visibility:auto]"
    >
      <ComposerPrimitive.Root className="aui-edit-composer-root ms-auto flex w-full max-w-[85%] cursor-text flex-col rounded-(--composer-radius) border border-foreground/10 bg-(--composer-bg) transition-[border-color] focus-within:border-foreground/25">
        <ComposerPrimitive.Input
          className="aui-edit-composer-input min-h-14 w-full resize-none bg-transparent px-4 pt-3 pb-1 text-base text-foreground outline-none"
          autoFocus
        />
        <div className="aui-edit-composer-footer mx-2.5 mb-2.5 flex items-center gap-1.5 self-end">
          <ComposerPrimitive.Cancel
            render={<Button variant="ghost" size="sm" className="h-8 px-3" />}
          >
            Cancel
          </ComposerPrimitive.Cancel>
          <ComposerPrimitive.Send
            render={<Button size="sm" className="h-8 px-3" />}
          >
            Update
          </ComposerPrimitive.Send>
        </div>
      </ComposerPrimitive.Root>
    </MessagePrimitive.Root>
  )
}

const BranchPicker: FC<BranchPickerPrimitive.Root.Props> = ({
  className,
  ...rest
}) => {
  return (
    <BranchPickerPrimitive.Root
      hideWhenSingleBranch
      className={cn(
        "aui-branch-picker-root -ms-2 me-2 inline-flex items-center text-xs text-muted-foreground",
        className
      )}
      {...rest}
    >
      <BranchPickerPrimitive.Previous
        render={<TooltipIconButton tooltip="Previous" />}
      >
        <ChevronLeftIcon />
      </BranchPickerPrimitive.Previous>
      <span className="aui-branch-picker-state font-medium">
        <BranchPickerPrimitive.Number /> / <BranchPickerPrimitive.Count />
      </span>
      <BranchPickerPrimitive.Next render={<TooltipIconButton tooltip="Next" />}>
        <ChevronRightIcon />
      </BranchPickerPrimitive.Next>
    </BranchPickerPrimitive.Root>
  )
}
