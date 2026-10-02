import { useEffect, useMemo, useRef, useState } from "react"
import { useAui, useAuiState } from "@assistant-ui/react"
import { TooltipIconButton } from "@/components/assistant-ui/elements/tooltip-icon-button"
import { Icon } from "@/components/forge/icon"
import {
  Command,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
  CommandShortcut,
} from "@/components/ui/command"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogTitle,
} from "@/components/ui/dialog"
import { Spinner } from "@/components/ui/spinner"
import {
  historyExcerpt,
  matchesHistory,
  useChatHistory,
  type HistoryEntry,
} from "./lib/chat-history"
import { useScreen } from "./lib/screen-store"
import type { AgentConnection } from "./lib/use-agent-workspace"

const dateFormat = new Intl.DateTimeFormat(undefined, {
  month: "short",
  day: "numeric",
})

/**
 * A chat-history button for the composer (⌘K / Ctrl+K opens it too): a
 * command palette with New chat and the user's conversations, searched by
 * title and by what was said. Use it as `composerTrailingActions`, wrapped
 * with the connection:
 *
 * ```tsx
 * const History = () => <ChatHistoryPalette adkUrl={url} appName="assistant" userId={id} />
 * ```
 *
 * Pass `shortcut={false}` when the app already uses ⌘K for its own palette.
 */
export function ChatHistoryPalette({
  shortcut = true,
  ...connection
}: AgentConnection & {
  /** Open it with ⌘K / Ctrl+K from anywhere on the page. Default true. */
  shortcut?: boolean
}) {
  const aui = useAui()
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState("")
  const triggerRef = useRef<HTMLButtonElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)
  const switching = useRef(false)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const history = useChatHistory(open, connection)
  const threadItems = useAuiState((s) => s.threads.threadItems)
  const currentId = useAuiState((s) => s.threadListItem.remoteId)
  const threadLoading = useAuiState((s) => s.threads.isLoading)

  const entries = useMemo(() => {
    const known = new Map(history.entries.map((entry) => [entry.id, entry]))
    for (const item of threadItems) {
      if (!item.remoteId || item.status === "archived") continue
      const entry = known.get(item.remoteId)
      if (entry) {
        if (item.title)
          known.set(item.remoteId, { ...entry, title: item.title })
      } else {
        known.set(item.remoteId, {
          id: item.remoteId,
          title: item.title || "Untitled chat",
          text: "",
          updatedAt: item.lastMessageAt?.getTime()
            ? item.lastMessageAt.getTime() / 1000
            : 0,
        })
      }
    }
    return [...known.values()]
      .sort((a, b) => b.updatedAt - a.updatedAt)
      .filter((entry) => matchesHistory(entry, query))
  }, [history.entries, threadItems, query])

  const changeOpen = (next: boolean) => {
    if (next) {
      setQuery("")
      setError(null)
    }
    setOpen(next)
  }
  useEffect(() => {
    if (!shortcut) return
    const onKey = (event: KeyboardEvent) => {
      if (
        (event.metaKey || event.ctrlKey) &&
        !event.altKey &&
        !event.shiftKey &&
        event.key.toLowerCase() === "k"
      ) {
        event.preventDefault()
        setQuery("")
        setError(null)
        setOpen((value) => !value)
      }
    }
    document.addEventListener("keydown", onKey)
    return () => document.removeEventListener("keydown", onKey)
  }, [shortcut])

  const select = async (id?: string) => {
    if (busy) return
    setBusy(true)
    setError(null)
    try {
      // Use the runtime promise so a failed switch keeps the palette open.
      const runtime = aui.threads.__internal_getAssistantRuntime?.()
      if (runtime) {
        if (id) await runtime.threads.switchToThread(id)
        else await runtime.threads.switchToNewThread()
      } else {
        if (id) aui.threads.switchToThread(id)
        else aui.threads.switchToNewThread()
      }
      useScreen.getState().closePanel()
      switching.current = true
      setOpen(false)
    } catch {
      setError("This chat couldn’t be opened. Try again.")
    } finally {
      setBusy(false)
    }
  }

  const newChatAction = (
    <>
      <CommandGroup>
        <CommandItem
          value="new-chat"
          disabled={busy}
          onSelect={() => void select()}
          className="min-h-11 gap-3 px-3 text-sm"
        >
          <Icon icon="plus" size={17} />
          <span>New chat</span>
          <CommandShortcut className="opacity-0 group-data-[selected=true]/command-item:opacity-100">
            ↵
          </CommandShortcut>
        </CommandItem>
      </CommandGroup>
      {!query.trim() && <div className="mx-2 my-2 h-px bg-border" />}
    </>
  )

  return (
    <>
      <TooltipIconButton
        ref={triggerRef}
        tooltip={shortcut ? "Search chats (⌘K / Ctrl+K)" : "Search chats"}
        aria-label="Search chat history"
        aria-haspopup="dialog"
        aria-expanded={open}
        onClick={() => changeOpen(true)}
        type="button"
        className="size-7 shrink-0 rounded-full text-muted-foreground hover:bg-muted hover:text-foreground"
      >
        <Icon icon="message" size={16} />
      </TooltipIconButton>
      <Dialog open={open} onOpenChange={changeOpen}>
        <DialogContent
          className="top-[12dvh] max-h-[78dvh] translate-y-0 gap-0 overflow-hidden p-0 sm:max-w-[580px]"
          showCloseButton={false}
          initialFocus={inputRef}
          finalFocus={() => {
            if (switching.current) {
              switching.current = false
              return (
                document.querySelector<HTMLTextAreaElement>(
                  'textarea[aria-label="Message input"]'
                ) ?? triggerRef.current
              )
            }
            return triggerRef.current
          }}
        >
          <DialogTitle className="sr-only">Chat history</DialogTitle>
          <DialogDescription className="sr-only">
            Search saved conversations or start a new chat. Use arrow keys to
            navigate and Enter to select.
          </DialogDescription>
          <Command shouldFilter={false} loop className="min-h-0">
            <div className="relative">
              <CommandInput
                ref={inputRef}
                value={query}
                onValueChange={setQuery}
                placeholder="Search chat history…"
                aria-label="Search chat history"
                className="pr-12"
              />
              <button
                type="button"
                aria-label="Close chat history"
                onClick={() => changeOpen(false)}
                className="absolute top-2 right-2 flex size-8 items-center justify-center rounded-md text-muted-foreground hover:bg-muted hover:text-foreground"
              >
                <Icon icon="close" size={15} />
              </button>
            </div>
            <CommandList
              className="max-h-[min(420px,55dvh)] p-2"
              aria-busy={history.loading || busy}
            >
              {!query.trim() && newChatAction}
              <CommandGroup
                heading={query.trim() ? "Matching chats" : "Recent chats"}
              >
                {entries.map((entry) => (
                  <HistoryItem
                    key={entry.id}
                    entry={entry}
                    query={query}
                    current={entry.id === currentId}
                    disabled={busy}
                    onSelect={() => void select(entry.id)}
                  />
                ))}
              </CommandGroup>
              {!entries.length && !(history.loading || threadLoading) && (
                <p
                  role="status"
                  className="px-4 py-8 text-center text-sm text-muted-foreground"
                >
                  {query.trim()
                    ? "No chats match your search."
                    : "Your conversations will appear here."}
                </p>
              )}
              {(history.loading || threadLoading) && (
                <p
                  role="status"
                  className="flex items-center justify-center gap-2 px-4 py-4 text-xs text-muted-foreground"
                >
                  <Spinner className="size-3" />
                  Searching saved chats…
                </p>
              )}
              {query.trim() && newChatAction}
            </CommandList>
            {(history.error || error) && (
              <div
                role="alert"
                className="flex items-center justify-between gap-3 border-t px-4 py-3 text-xs text-danger-foreground"
              >
                <span>{error || history.error}</span>
                {history.error && (
                  <button
                    type="button"
                    onClick={history.retry}
                    className="rounded px-2 py-1 font-medium hover:bg-muted"
                  >
                    Retry
                  </button>
                )}
              </div>
            )}
            <div className="flex items-center gap-4 border-t px-4 py-2.5 text-2xs text-muted-foreground">
              <span>↑↓ Navigate</span>
              <span>↵ Open</span>
              <span className="ml-auto">Esc Close</span>
            </div>
          </Command>
        </DialogContent>
      </Dialog>
    </>
  )
}

function HistoryItem({
  entry,
  query,
  current,
  disabled,
  onSelect,
}: {
  entry: HistoryEntry
  query: string
  current: boolean
  disabled: boolean
  onSelect: () => void
}) {
  const excerpt = historyExcerpt(entry, query)
  return (
    <CommandItem
      value={`chat:${entry.id}`}
      disabled={disabled}
      onSelect={onSelect}
      className="min-h-12 gap-3 px-3 py-2.5"
    >
      <Icon icon="message" size={16} className="text-muted-foreground" />
      <span className="min-w-0 flex-1">
        <span className="block truncate text-sm">{entry.title}</span>
        {excerpt && (
          <span className="mt-1 block truncate text-xs text-muted-foreground">
            {excerpt}
          </span>
        )}
      </span>
      <CommandShortcut className="shrink-0 text-2xs tracking-normal">
        {current
          ? "Current"
          : entry.updatedAt > 0
            ? dateFormat.format(new Date(entry.updatedAt * 1000))
            : ""}
      </CommandShortcut>
    </CommandItem>
  )
}
