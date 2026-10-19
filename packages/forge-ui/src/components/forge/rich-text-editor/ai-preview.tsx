import * as React from "react"
import {
  AiChat02Icon,
  Alert02Icon,
  SparklesIcon,
} from "@hugeicons/core-free-icons"
import { getHTMLFromFragment, type Editor } from "@tiptap/core"
import { cn } from "cn"

import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Kbd } from "@/components/ui/kbd"
import { Skeleton } from "@/components/ui/skeleton"
import { Spinner } from "@/components/ui/spinner"
import { Icon } from "../icon"
import type { AIController } from "./ai/ai-controller"
import type { AIResultMode, AIState } from "./ai/ai-types"
import { useRichTextEditorContext } from "./context"
import { closeOpenMarkdown, parseMarkdown } from "./core/markdown"
import { formatShortcut } from "./shortcuts"
import type { UrlPolicy } from "./types"

/** What a custom AI panel (`components.AIPreview`) receives. */
export interface AIPreviewProps {
  state: AIState
  ai: AIController
  editor: Editor
}

const ANNOUNCEMENTS: Partial<Record<AIState["status"], string>> = {
  running: "The AI is writing.",
  ready: "The AI's answer is ready. Accept it or discard it.",
  conflict:
    "The text changed while the AI worked. Choose where the answer goes.",
}

/**
 * Renders streamed Markdown with the editor's own schema and typography,
 * without touching the document. Unfinished syntax is closed first.
 */
function MarkdownPreview({
  markdown,
  editor,
  policy,
}: {
  markdown: string
  editor: Editor
  policy: UrlPolicy
}) {
  const html = React.useMemo(() => {
    try {
      const { doc } = parseMarkdown(editor, closeOpenMarkdown(markdown), policy)
      return getHTMLFromFragment(
        editor.schema.nodeFromJSON(doc).content,
        editor.schema
      )
    } catch {
      return null
    }
  }, [markdown, editor, policy])
  if (html === null) {
    return (
      <pre className="font-mono text-xs whitespace-pre-wrap">{markdown}</pre>
    )
  }
  return (
    <div className="rich-text">
      {/* The editor's schema produced this HTML, so it holds only what the
          document could: no scripts, and links and images the policy allows. */}
      <div
        className="ProseMirror rich-text-preview"
        dangerouslySetInnerHTML={{ __html: html }}
      />
    </div>
  )
}

function acceptLabel(mode: AIResultMode, hasSelection: boolean) {
  if (mode === "replace") return "Replace selection"
  if (mode === "append") return "Add to the end"
  return hasSelection ? "Insert below" : "Insert"
}

/**
 * The AI panel under the text: the "Ask AI…" prompt, the answer as it
 * streams, then Accept, Try again and Discard. A conflict (the text changed
 * meanwhile) offers the caret instead. Status changes are announced, ⌘Enter
 * accepts and Escape cancels or discards.
 */
export function AIResponsePreview({ className }: { className?: string }) {
  const { editor, ai, controller, urlPolicy } = useRichTextEditorContext()
  const state = React.useSyncExternalStore(ai.subscribe, ai.getState)
  const panelRef = React.useRef<HTMLDivElement>(null)
  const primaryRef = React.useRef<HTMLButtonElement>(null)
  const [prompt, setPrompt] = React.useState("")
  const [copied, setCopied] = React.useState(false)
  const { status } = state

  // With focus in the panel (Stop, say), it moves to the answer's main
  // button once that exists; someone typing in the text keeps typing.
  React.useEffect(() => {
    const active = document.activeElement
    const inPanel = Boolean(active && panelRef.current?.contains(active))
    if (
      (status === "ready" || status === "conflict") &&
      (inPanel || active === document.body)
    ) {
      primaryRef.current?.focus()
    }
  }, [status])

  const close = (action: () => void) => {
    const inPanel = panelRef.current?.contains(document.activeElement)
    action()
    setPrompt("")
    if (inPanel) controller.focus()
  }

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(state.markdown)
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    } catch {
      // The buttons stay; the person can still accept or select the text.
    }
  }

  const accept = (target?: AIResultMode | "cursor") => {
    const inPanel = panelRef.current?.contains(document.activeElement)
    if (ai.accept(target) && inPanel) controller.focus()
  }

  const onKeyDown = (event: React.KeyboardEvent) => {
    if (event.key === "Escape") {
      event.preventDefault()
      close(() => (status === "running" ? ai.cancel() : ai.reject()))
    } else if (
      event.key === "Enter" &&
      (event.metaKey || event.ctrlKey) &&
      status === "ready"
    ) {
      event.preventDefault()
      accept()
    }
  }

  const announcement =
    status === "error"
      ? `The AI request failed: ${state.error ?? ""}`
      : (ANNOUNCEMENTS[status] ?? "")

  return (
    <>
      <div role="status" aria-live="polite" className="sr-only">
        {announcement}
      </div>
      {status !== "idle" && (
        <div
          ref={panelRef}
          data-slot="rich-text-ai"
          role="region"
          aria-label="AI answer"
          onKeyDown={onKeyDown}
          className={cn(
            "flex flex-col gap-2 border-t border-border bg-muted/40 px-3 py-2.5",
            className
          )}
        >
          {status === "prompt" ? (
            <form
              className="flex items-center gap-2"
              onSubmit={(event) => {
                event.preventDefault()
                if (!prompt.trim()) return
                void ai.submitPrompt(prompt)
                // The next prompt box starts empty.
                setPrompt("")
              }}
            >
              <Icon
                icon={AiChat02Icon}
                className="shrink-0 text-muted-foreground"
              />
              <Input
                autoFocus
                value={prompt}
                onChange={(event) => setPrompt(event.target.value)}
                placeholder={
                  state.action?.placeholder ??
                  (state.hasSelection
                    ? "What should the AI do with the selection?"
                    : "What should the AI write here?")
                }
                aria-label={`What ${state.action?.label.replace(/…$/, "") ?? "the AI"} should do`}
                className="h-7 flex-1 bg-background"
              />
              <Button type="submit" size="sm" disabled={!prompt.trim()}>
                Generate
              </Button>
              <Button
                type="button"
                variant="ghost"
                size="sm"
                onClick={() => close(() => ai.cancel())}
              >
                Cancel
              </Button>
            </form>
          ) : (
            <>
              <div className="flex items-center gap-2 text-xs">
                {status === "running" ? (
                  <Spinner className="text-muted-foreground" />
                ) : status === "error" || status === "conflict" ? (
                  <Icon
                    icon={Alert02Icon}
                    size={15}
                    className="text-destructive"
                  />
                ) : (
                  <Icon
                    icon={SparklesIcon}
                    size={15}
                    className="text-muted-foreground"
                  />
                )}
                <span className="font-medium text-foreground">
                  {state.action?.label.replace(/…$/, "") ?? "AI"}
                </span>
                {state.instruction && (
                  <span className="min-w-0 truncate text-muted-foreground">
                    “{state.instruction}”
                  </span>
                )}
                <span className="ml-auto text-2xs text-subtle">
                  {status === "running"
                    ? "Writing…"
                    : status === "ready"
                      ? "Review before it goes in"
                      : null}
                </span>
              </div>

              {status === "error" ? (
                <p className="text-xs text-destructive">{state.error}</p>
              ) : status === "running" && !state.markdown ? (
                <div aria-hidden className="flex flex-col gap-1.5 py-1">
                  <Skeleton className="h-3.5 w-3/4" />
                  <Skeleton className="h-3.5 w-1/2" />
                </div>
              ) : (
                <div
                  tabIndex={0}
                  aria-label="Answer preview"
                  className="max-h-64 overflow-y-auto rounded-(--radius-card) border border-border bg-background"
                >
                  <MarkdownPreview
                    markdown={state.markdown}
                    editor={editor}
                    policy={urlPolicy()}
                  />
                </div>
              )}

              {status === "conflict" && (
                <p className="text-xs text-muted-foreground">
                  The text this was for changed while the AI worked, so it
                  wasn't overwritten. Put the answer at the caret instead, or
                  copy it.
                </p>
              )}

              <div className="flex flex-wrap items-center gap-1.5">
                {status === "running" && (
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    onClick={() => close(() => ai.cancel())}
                  >
                    <Icon icon="stop" data-icon="inline-start" />
                    Stop
                  </Button>
                )}
                {status === "ready" &&
                  (state.mode === "preview" ? (
                    <>
                      {state.hasSelection && (
                        <Button
                          ref={primaryRef}
                          type="button"
                          size="sm"
                          onClick={() => accept("replace")}
                        >
                          Replace selection
                        </Button>
                      )}
                      <Button
                        ref={state.hasSelection ? undefined : primaryRef}
                        type="button"
                        size="sm"
                        variant={state.hasSelection ? "outline" : "default"}
                        onClick={() => accept("insert")}
                      >
                        {state.hasSelection ? "Insert below" : "Insert"}
                      </Button>
                    </>
                  ) : (
                    <Button
                      ref={primaryRef}
                      type="button"
                      size="sm"
                      onClick={() => accept()}
                    >
                      {acceptLabel(state.mode, state.hasSelection)}
                      <Kbd className="ml-1 bg-transparent text-current opacity-70">
                        {formatShortcut("Mod-Enter")}
                      </Kbd>
                    </Button>
                  ))}
                {status === "conflict" && (
                  <Button
                    ref={primaryRef}
                    type="button"
                    size="sm"
                    onClick={() => accept("cursor")}
                  >
                    Insert at caret
                  </Button>
                )}
                {(status === "ready" || status === "conflict") && (
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    onClick={() => void copy()}
                  >
                    <Icon icon="copy" data-icon="inline-start" />
                    {copied ? "Copied" : "Copy"}
                  </Button>
                )}
                {(status === "ready" || status === "error") && (
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    onClick={() => void ai.regenerate()}
                  >
                    <Icon icon="refresh" data-icon="inline-start" />
                    Try again
                  </Button>
                )}
                {status !== "running" && (
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    className="ml-auto"
                    onClick={() => close(() => ai.reject())}
                  >
                    {status === "error" ? "Dismiss" : "Discard"}
                  </Button>
                )}
              </div>
            </>
          )}
        </div>
      )}
    </>
  )
}
