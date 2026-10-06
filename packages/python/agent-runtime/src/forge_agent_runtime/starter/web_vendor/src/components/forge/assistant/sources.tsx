import * as React from "react"
import { useAuiState, type ThreadMessage } from "@assistant-ui/react"
import { cn } from "cn"

import { MarkdownText } from "@/components/assistant-ui/elements/markdown-text"
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible"
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover"
import { Icon } from "@/components/forge/icon"

import { useAssistantSettings } from "./assistant-context"
import { REF_HREF, citedRefs, remarkSourceRefs } from "./source-refs"

/*
 * Cited sources. An agent's tools return passages that each carry a ref
 * ("S3"); the agent cites them inline as "[S3]". The tool results stream with
 * the answer, in the same message, so the console resolves every ref from
 * them: no second channel, and the sources come back with the conversation.
 */

/** A document an answer can cite; its passages are listed together. */
export type AssistantSourceDocument = {
  /** Identifies the document. */
  key: string
  /** Its name, e.g. the filename. */
  title: string
  /** Where it's from, e.g. the team. */
  subtitle?: string
  /** Opens the document, in a new tab. */
  href?: string
}

/** A passage a tool returned, which the agent cites by `ref`. */
export type AssistantSource = {
  /** What the agent writes in brackets, e.g. "S3". */
  ref: string
  document: AssistantSourceDocument
  /** Where in the document: section, page, sheet. */
  location?: string
  /** The passage, shortened. */
  excerpt?: string
}

/** What a search call looked through and found, for its card. */
export type AssistantSearch = {
  /** What it searched, by name, e.g. the knowledge bases. */
  searched: string[]
  /** The passages it found; none when nothing was relevant enough. */
  found: number
  /** The documents they're from. */
  documents: number
}

/**
 * How answers cite what the agent's tools found. Make it once (at module
 * scope or in `useMemo`): each message's sources are read once per config.
 */
export type AssistantSourcesConfig = {
  /** The sources in a finished tool call's result; [] for other tools. */
  fromToolCall: (toolName: string, result: unknown) => AssistantSource[]
  /**
   * The search a finished tool call made, which its card says in place of
   * "Used tool: <name>" ("Searched Claims knowledge · 5 passages in 3
   * documents"); undefined for calls that aren't searches.
   */
  searchOf?: (toolName: string, result: unknown) => AssistantSearch | undefined
}

/* -------------------------------------------------------------------------- */
/* Where the refs point                                                       */
/* -------------------------------------------------------------------------- */

type Cached = { config: AssistantSourcesConfig; sources: AssistantSource[] }
// A message's sources, read once per version of it.
const perMessage = new WeakMap<ThreadMessage, Cached>()

function sourcesOf(
  message: ThreadMessage,
  config: AssistantSourcesConfig
): AssistantSource[] {
  const cached = perMessage.get(message)
  if (cached?.config === config) return cached.sources
  const sources: AssistantSource[] = []
  if (message.role === "assistant") {
    for (const part of message.content) {
      if (part.type !== "tool-call" || part.result === undefined) continue
      if (part.isError) continue
      try {
        sources.push(...config.fromToolCall(part.toolName, part.result))
      } catch {
        // A result the config can't read cites nothing.
      }
    }
  }
  perMessage.set(message, { config, sources })
  return sources
}

/** Every source the conversation's tools returned, by ref. */
function useThreadSources() {
  const config = useAssistantSettings().sources
  const messages = useAuiState((s) => s.thread.messages)
  return React.useMemo(() => {
    const byRef = new Map<string, AssistantSource>()
    if (config) {
      for (const message of messages) {
        for (const source of sourcesOf(message, config)) {
          byRef.set(source.ref, source)
        }
      }
    }
    return byRef
  }, [messages, config])
}

/** The refs this message cites, in order: [S7, S2] numbers S7 as 1. */
function useMessageCitations() {
  const text = useAuiState((s) =>
    s.message.content
      .map((part) => (part.type === "text" ? part.text : ""))
      .join("\n")
  )
  return React.useMemo(() => citedRefs(text), [text])
}

/* -------------------------------------------------------------------------- */
/* Citations in the text                                                      */
/* -------------------------------------------------------------------------- */

const chip =
  "inline-flex h-4 min-w-4 shrink-0 items-center justify-center rounded-(--radius-chip) bg-muted px-1 font-sans text-3xs leading-none font-medium text-muted-foreground tabular-nums"

/**
 * A citation: its number in this answer, opening the passage behind it on
 * hover or press. A ref no tool returned stays the text the agent wrote.
 */
function SourceRef({ id }: { id: string }) {
  const byRef = useThreadSources()
  const cited = useMessageCitations()
  const source = byRef.get(id)
  const number = cited.indexOf(id) + 1
  if (!source || number === 0) return <>{`[${id}]`}</>
  return (
    <Popover>
      {/* A span, not a button: an inline box keeps the no-break space
          before it, so a citation never wraps away from its word. */}
      <PopoverTrigger
        openOnHover
        delay={150}
        nativeButton={false}
        render={<span />}
        aria-label={`Source ${number}: ${source.document.title}`}
        className="relative -top-px ms-0.5 cursor-pointer rounded-(--radius-chip) bg-muted px-1 py-px font-sans text-3xs font-medium whitespace-nowrap text-muted-foreground tabular-nums transition-colors duration-100 hover:bg-accent hover:text-foreground data-popup-open:bg-accent data-popup-open:text-foreground"
      >
        {number}
      </PopoverTrigger>
      <PopoverContent side="top" align="start" className="w-80 gap-2">
        <SourceCard source={source} number={number} />
      </PopoverContent>
    </Popover>
  )
}

function SourceCard({
  source,
  number,
}: {
  source: AssistantSource
  number: number
}) {
  const { document } = source
  const where = [document.subtitle, source.location].filter(Boolean).join(" · ")
  return (
    <>
      <div className="flex min-w-0 items-start gap-2">
        <span className={cn(chip, "mt-px")}>{number}</span>
        <div className="grid min-w-0 gap-0.5">
          <p className="truncate font-medium text-foreground">
            {document.title}
          </p>
          {where && <p className="text-muted-foreground">{where}</p>}
        </div>
      </div>
      {source.excerpt && (
        <p className="line-clamp-4 text-muted-foreground">{source.excerpt}</p>
      )}
      {document.href && (
        <a
          href={document.href}
          target="_blank"
          rel="noreferrer"
          className="inline-flex items-center gap-1 self-start font-medium text-foreground underline-offset-2 hover:underline"
        >
          Open document
          <Icon icon="external" size={13} />
        </a>
      )}
    </>
  )
}

/** Markdown links: citations from `remarkSourceRefs`, else a plain link. */
function SourceAnchor({
  href,
  className,
  children,
  ...props
}: React.ComponentProps<"a">) {
  if (href?.startsWith(REF_HREF))
    return <SourceRef id={href.slice(REF_HREF.length)} />
  return (
    <a
      href={href}
      className={cn(
        "aui-md-a text-primary underline underline-offset-2 hover:text-primary/80",
        className
      )}
      {...props}
    >
      {children}
    </a>
  )
}

const sourceComponents = { a: SourceAnchor }
const sourcePlugins = [remarkSourceRefs]

/** An answer's text, its "[S3]" citations drawn as numbered sources. */
export function SourcedMarkdownText() {
  const config = useAssistantSettings().sources
  if (!config) return <MarkdownText />
  return (
    <MarkdownText components={sourceComponents} remarkPlugins={sourcePlugins} />
  )
}

/* -------------------------------------------------------------------------- */
/* Sources under the answer                                                   */
/* -------------------------------------------------------------------------- */

type DocumentGroup = {
  document: AssistantSourceDocument
  sources: AssistantSource[]
}

function groupByDocument(sources: AssistantSource[]): DocumentGroup[] {
  const groups = new Map<string, DocumentGroup>()
  for (const source of sources) {
    const group = groups.get(source.document.key)
    if (group) group.sources.push(source)
    else
      groups.set(source.document.key, {
        document: source.document,
        sources: [source],
      })
  }
  return [...groups.values()]
}

function DocumentTitle({ document }: { document: AssistantSourceDocument }) {
  return document.href ? (
    <a
      href={document.href}
      target="_blank"
      rel="noreferrer"
      className="truncate font-medium text-foreground underline-offset-2 hover:underline"
    >
      {document.title}
    </a>
  ) : (
    <span className="truncate font-medium text-foreground">
      {document.title}
    </span>
  )
}

function DocumentRow({
  group,
  cited,
}: {
  group: DocumentGroup
  cited: boolean
}) {
  const locations = [
    ...new Set(group.sources.map((s) => s.location).filter(Boolean)),
  ]
  return (
    <li className="flex min-w-0 items-start gap-2 text-xs">
      {cited ? (
        <span className="mt-px flex shrink-0 gap-0.5">
          {group.sources.map((source) => (
            <SourceRef key={source.ref} id={source.ref} />
          ))}
        </span>
      ) : (
        <Icon
          icon="file"
          size={14}
          className="mt-px shrink-0 text-muted-foreground"
        />
      )}
      <div className="grid min-w-0 flex-1 gap-px">
        <div className="flex min-w-0 items-baseline gap-2">
          <DocumentTitle document={group.document} />
          {group.document.subtitle && (
            <span className="ms-auto shrink-0 text-2xs text-subtle">
              {group.document.subtitle}
            </span>
          )}
        </div>
        {locations.length > 0 && (
          <span className="truncate text-2xs text-muted-foreground">
            {locations.join(" · ")}
          </span>
        )}
      </div>
    </li>
  )
}

const plural = (count: number, one: string) =>
  `${count} ${one}${count === 1 ? "" : "s"}`

/* -------------------------------------------------------------------------- */
/* Search calls                                                               */
/* -------------------------------------------------------------------------- */

/**
 * A search call's card label: what it searched and what it found, e.g.
 * "Searched **Member knowledge** and **Claims knowledge** · 5 passages in 3
 * documents", or "· nothing relevant" when it found nothing to answer from.
 */
export function SearchLabel({ search }: { search: AssistantSearch }) {
  const [first, second, ...rest] = search.searched
  return (
    <>
      Searched {first ? <b>{first}</b> : "the knowledge base"}
      {second &&
        (rest.length ? (
          <> and {rest.length + 1} more</>
        ) : (
          <>
            {" "}
            and <b>{second}</b>
          </>
        ))}
      <span className="text-subtle">
        {" · "}
        {search.found
          ? `${plural(search.found, "passage")} in ${plural(search.documents, "document")}`
          : "nothing relevant"}
      </span>
    </>
  )
}

/**
 * The documents under an answer: the ones it cites, numbered as in the text,
 * then the others its searches found. Shown once the answer is complete.
 */
export function MessageSources() {
  const config = useAssistantSettings().sources
  const message = useAuiState((s) => s.message)
  const byRef = useThreadSources()
  const cited = useMessageCitations()
  if (!config || message.role !== "assistant") return null
  if (message.status?.type === "running") return null

  const citedSources = cited.flatMap((ref) => byRef.get(ref) ?? [])
  const citedGroups = groupByDocument(citedSources)
  const citedKeys = new Set(citedGroups.map((g) => g.document.key))
  const searched = groupByDocument(sourcesOf(message, config)).filter(
    (g) => !citedKeys.has(g.document.key)
  )
  if (!citedGroups.length && !searched.length) return null

  return (
    <section
      aria-label="Sources"
      data-slot="assistant-message-sources"
      className="mt-3 grid gap-1.5 border-t border-border pt-2.5"
    >
      {citedGroups.length > 0 && (
        <>
          <p className="text-2xs font-medium text-muted-foreground">Sources</p>
          <ul className="grid gap-1.5">
            {citedGroups.map((group) => (
              <DocumentRow key={group.document.key} group={group} cited />
            ))}
          </ul>
        </>
      )}
      {searched.length > 0 && (
        <Collapsible>
          <CollapsibleTrigger className="group/searched inline-flex items-center gap-1 text-2xs font-medium text-muted-foreground hover:text-foreground">
            {citedGroups.length ? "Also searched" : "Searched"}{" "}
            {plural(searched.length, "document")}
            <Icon
              icon="down"
              size={13}
              className="transition-transform duration-150 group-data-panel-open/searched:rotate-180"
            />
          </CollapsibleTrigger>
          <CollapsibleContent>
            <ul className="mt-1.5 grid gap-1.5">
              {searched.map((group) => (
                <DocumentRow
                  key={group.document.key}
                  group={group}
                  cited={false}
                />
              ))}
            </ul>
          </CollapsibleContent>
        </Collapsible>
      )}
    </section>
  )
}
