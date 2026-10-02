import * as React from "react"
import { useAuiState } from "@assistant-ui/react"
import { useAdkArtifacts } from "@assistant-ui/react-google-adk"
import {
  Copy01Icon,
  Download04Icon,
  FileCodeIcon,
  Tick02Icon,
} from "@hugeicons/core-free-icons"
import Markdown from "react-markdown"
import remarkGfm from "remark-gfm"
import { cn } from "cn"

import "./canvas.css"
import { TooltipIconButton } from "@/components/assistant-ui/elements/tooltip-icon-button"
import { useAssistantSettings } from "@/components/forge/assistant"
import { Icon } from "@/components/forge/icon"
import { SidePanel } from "./side-panel"
import { Button } from "@/components/ui/button"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { Skeleton } from "@/components/ui/skeleton"
import { useCopyToClipboard } from "@/hooks/use-copy-to-clipboard"
import { useCanvas } from "./lib/canvas"
import {
  artifactText,
  downloadDocument,
  highlight,
  isMarkdown,
} from "./lib/documents"

type Loaded =
  { key: string; text: string } | { key: string; error: string } | undefined

/**
 * The document the canvas shows, loaded from the chat API's artifacts: the
 * version picked, or the latest, reloaded as the agent saves new versions
 * (`useAdkArtifacts` updates with each save).
 */
function useDocument() {
  const { artifacts: api } = useAssistantSettings()
  const sessionId = useAuiState((s) => s.threadListItem.remoteId)
  const latest = useAdkArtifacts()
  const { filename, version } = useCanvas()
  const latestVersion = filename ? latest[filename] : undefined
  const shown = version ?? latestVersion
  const [versions, setVersions] = React.useState<number[]>([])
  const [loaded, setLoaded] = React.useState<Loaded>()
  const key = `${sessionId}/${filename}/${shown}`

  React.useEffect(() => {
    if (!api || !sessionId || !filename) return
    let current = true
    api
      .listVersions(sessionId, filename)
      .then((list) => current && setVersions(list))
      .catch(() => current && setVersions([]))
    return () => {
      current = false
    }
  }, [api, sessionId, filename, latestVersion])

  React.useEffect(() => {
    if (!api || !sessionId || !filename) return
    let current = true
    api
      .load(sessionId, filename, shown)
      .then((data) => {
        const text = artifactText(data)
        if (!current) return
        setLoaded(
          text === undefined
            ? { key, error: "This file isn't text, so it can't be shown here." }
            : { key, text }
        )
      })
      .catch(
        (error: unknown) =>
          current &&
          setLoaded({
            key,
            error: error instanceof Error ? error.message : String(error),
          })
      )
    return () => {
      current = false
    }
  }, [api, sessionId, filename, shown, key])

  return {
    filename,
    shown,
    latestVersion,
    versions,
    // Only what's loaded for what's picked; the last one while the next loads.
    document: loaded?.key === key ? loaded : undefined,
  }
}

function VersionPicker({
  versions,
  shown,
  latest,
}: {
  versions: number[]
  shown: number | undefined
  latest: number | undefined
}) {
  const showVersion = useCanvas((state) => state.showVersion)
  if (versions.length < 2 || shown === undefined) return null
  return (
    <DropdownMenu>
      <DropdownMenuTrigger
        render={
          <Button
            variant="ghost"
            size="xs"
            className="h-7 px-2 text-xs font-normal text-muted-foreground tabular-nums"
          />
        }
        aria-label={`Version ${shown + 1}`}
      >
        v{shown + 1}
        {shown === latest && (
          <span className="text-muted-foreground/60">latest</span>
        )}
        <Icon icon="down" data-icon="inline-end" />
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-40">
        <DropdownMenuGroup>
          <DropdownMenuLabel>Versions</DropdownMenuLabel>
          <DropdownMenuRadioGroup
            value={String(shown)}
            onValueChange={(value) =>
              showVersion(Number(value) === latest ? null : Number(value))
            }
          >
            {[...versions].reverse().map((each) => (
              <DropdownMenuRadioItem
                key={each}
                value={String(each)}
                closeOnClick
              >
                <span className="tabular-nums">v{each + 1}</span>
                {each === latest && (
                  <span className="text-muted-foreground">latest</span>
                )}
              </DropdownMenuRadioItem>
            ))}
          </DropdownMenuRadioGroup>
        </DropdownMenuGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

function CodeView({ filename, text }: { filename: string; text: string }) {
  const html = React.useMemo(() => highlight(filename, text), [filename, text])
  const lines = text.split("\n").length
  return (
    <div className="canvas-code flex min-w-max font-mono text-xs leading-5">
      <div
        aria-hidden
        className="sticky start-0 shrink-0 bg-background py-3 ps-3 pe-3 text-end text-muted-foreground/50 tabular-nums select-none"
      >
        {Array.from({ length: lines }, (_, index) => (
          <div key={index}>{index + 1}</div>
        ))}
      </div>
      <pre className="py-3 pe-6 text-foreground">
        <code dangerouslySetInnerHTML={{ __html: html }} />
      </pre>
    </div>
  )
}

function ViewSwitch({
  view,
  onChange,
}: {
  view: "preview" | "source"
  onChange: (view: "preview" | "source") => void
}) {
  return (
    <div className="flex rounded-md bg-muted p-0.5 text-xs">
      {(["preview", "source"] as const).map((each) => (
        <button
          key={each}
          type="button"
          aria-pressed={view === each}
          onClick={() => onChange(each)}
          className="rounded-[5px] px-2 py-0.5 text-muted-foreground capitalize transition-colors aria-pressed:bg-background aria-pressed:text-foreground aria-pressed:shadow-xs"
        >
          {each}
        </button>
      ))}
    </div>
  )
}

/**
 * Beside the chat while `useScreen`'s panel is "canvas": a document the agent
 * saved (write_document), highlighted by its extension, with Markdown
 * previewed; its versions, and copy and download.
 */
export function CanvasPanel() {
  const { filename, shown, latestVersion, versions, document } = useDocument()
  const { isCopied, copyToClipboard } = useCopyToClipboard()
  const [view, setView] = React.useState<"preview" | "source">("preview")
  const text = document && "text" in document ? document.text : undefined
  const markdown = filename ? isMarkdown(filename) : false

  return (
    <SidePanel
      panel="canvas"
      title={<span className="font-mono">{filename ?? "Canvas"}</span>}
      icon={FileCodeIcon}
      className="w-[min(46rem,52vw)] bg-background"
      actions={
        filename && (
          <>
            <VersionPicker
              versions={versions}
              shown={shown}
              latest={latestVersion}
            />
            {markdown && <ViewSwitch view={view} onChange={setView} />}
            <TooltipIconButton
              tooltip={isCopied ? "Copied" : "Copy"}
              side="bottom"
              disabled={text === undefined}
              className="size-7 rounded-md p-0 text-muted-foreground hover:text-foreground"
              onClick={() => text !== undefined && copyToClipboard(text)}
            >
              <Icon icon={isCopied ? Tick02Icon : Copy01Icon} size={14} />
            </TooltipIconButton>
            <TooltipIconButton
              tooltip="Download"
              side="bottom"
              disabled={text === undefined}
              className="size-7 rounded-md p-0 text-muted-foreground hover:text-foreground"
              onClick={() =>
                text !== undefined && downloadDocument(filename, text)
              }
            >
              <Icon icon={Download04Icon} size={14} />
            </TooltipIconButton>
          </>
        )
      }
    >
      <div className={cn("min-h-0 flex-1 overflow-auto", !filename && "p-4")}>
        {!filename ? (
          <p className="text-sm text-muted-foreground">
            Nothing open. When the assistant writes a document or a long piece
            of code, it opens here.
          </p>
        ) : !document ? (
          <div className="flex flex-col gap-2 p-5" aria-busy>
            <Skeleton className="h-4 w-2/3" />
            <Skeleton className="h-4 w-5/6" />
            <Skeleton className="h-4 w-1/2" />
          </div>
        ) : "error" in document ? (
          <p className="p-4 text-sm text-destructive">{document.error}</p>
        ) : markdown && view === "preview" ? (
          <article className="canvas-prose mx-auto max-w-2xl px-6 py-5">
            <Markdown remarkPlugins={[remarkGfm]}>{document.text}</Markdown>
          </article>
        ) : (
          <CodeView filename={filename} text={document.text} />
        )}
      </div>
    </SidePanel>
  )
}
