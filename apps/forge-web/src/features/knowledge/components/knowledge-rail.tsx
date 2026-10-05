import * as React from "react"
import { cn } from "cn"

import { RunStateIcon } from "@/components/forge/status"
import { Avatar, AvatarFallback } from "@/components/ui/avatar"
import { Skeleton } from "@/components/ui/skeleton"
import type { DocumentSummary, KnowledgeDocument } from "../lib/api"
import {
  formatBytes,
  formatCount,
  formatDate,
  formatWhen,
  INDEX_STATE_KEYS,
  INDEX_STATES,
  readyShare,
  stateOf,
  totalsByState,
  type IndexState,
} from "../lib/knowledge"
import { IndexBar } from "./index-bar"

/**
 * The page's right-hand column, on a hairline beside the documents: what
 * agents can search now, who has added documents, and what
 * happened to the latest ones.
 */
export function KnowledgeRail({
  summary,
  recent,
  nameOf,
  me,
  onOpen,
  className,
}: {
  summary: DocumentSummary | undefined
  /** The latest documents, newest first. */
  recent: KnowledgeDocument[] | undefined
  nameOf: (user: string) => string
  me: string | undefined
  /** Opens a document in the viewer. */
  onOpen: (document: KnowledgeDocument) => void
  className?: string
}) {
  return (
    <aside
      aria-label="Knowledge overview"
      className={cn("flex min-w-0 flex-col", className)}
    >
      <IndexOverview summary={summary} />
      <Contributors summary={summary} nameOf={nameOf} me={me} />
      <RecentActivity recent={recent} nameOf={nameOf} me={me} onOpen={onOpen} />
    </aside>
  )
}

function RailSection({
  id,
  title,
  meta,
  children,
}: {
  id: string
  title: string
  meta?: React.ReactNode
  children: React.ReactNode
}) {
  return (
    <section
      aria-labelledby={id}
      className="border-t py-5 first:border-t-0 first:pt-0"
    >
      <div className="mb-3.5 flex items-baseline justify-between gap-3">
        <h2 id={id} className="text-xs font-medium text-foreground">
          {title}
        </h2>
        {meta !== undefined && (
          <span className="text-2xs text-subtle tabular-nums">{meta}</span>
        )}
      </div>
      {children}
    </section>
  )
}

function IndexOverview({ summary }: { summary: DocumentSummary | undefined }) {
  const states = totalsByState(summary)
  const total = summary?.totals.documents ?? 0
  return (
    <RailSection
      id="knowledge-index"
      title="Knowledge index"
      meta={summary ? `${readyShare(states)}% ready` : undefined}
    >
      {summary ? (
        <>
          <IndexBar totals={states} />
          <p className="mt-2.5 text-xs text-muted-foreground">
            <strong className="font-medium text-foreground tabular-nums">
              {formatCount(states.ready.documents)}
            </strong>{" "}
            of {formatCount(total)}{" "}
            {total === 1 ? "document is" : "documents are"} searchable
          </p>
          <ul className="mt-4 grid grid-cols-2 gap-x-4 gap-y-2">
            {INDEX_STATE_KEYS.filter(
              (key) => key !== "unknown" || states.unknown.documents > 0
            ).map((key) => (
              <li
                key={key}
                className="flex items-center gap-2 text-2xs text-muted-foreground"
              >
                <span
                  aria-hidden="true"
                  className={cn(
                    "size-2 shrink-0 rounded-[2px]",
                    INDEX_STATES[key].bar
                  )}
                />
                <span className="truncate">{INDEX_STATES[key].label}</span>
                <span className="ml-auto font-medium text-foreground tabular-nums">
                  {formatCount(states[key].documents)}
                </span>
              </li>
            ))}
          </ul>
          <dl className="mt-4 border-t text-xs">
            <Fact label="Chunks embedded">
              {formatCount(summary.totals.chunks)}
            </Fact>
            <Fact label="Stored">{formatBytes(summary.totals.size_bytes)}</Fact>
          </dl>
        </>
      ) : (
        <div className="grid gap-3" aria-busy="true">
          <Skeleton className="h-1.5 w-full rounded-full" />
          <Skeleton className="h-3 w-40" />
          <Skeleton className="h-12 w-full" />
        </div>
      )}
    </RailSection>
  )
}

function Fact({
  label,
  children,
}: {
  label: string
  children: React.ReactNode
}) {
  return (
    <div className="flex items-baseline justify-between gap-3 border-b py-2 last:border-b-0">
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="font-medium text-foreground tabular-nums">{children}</dd>
    </div>
  )
}

const initials = (name: string) => {
  const words = name.split(/[\s@._-]+/).filter(Boolean)
  return ((words[0]?.[0] ?? "") + (words[1]?.[0] ?? "")).toUpperCase() || "?"
}

function Person({ name }: { name: string }) {
  return (
    <Avatar size="sm" aria-hidden="true">
      <AvatarFallback className="text-3xs font-medium text-foreground">
        {initials(name)}
      </AvatarFallback>
    </Avatar>
  )
}

function Contributors({
  summary,
  nameOf,
  me,
}: {
  summary: DocumentSummary | undefined
  nameOf: (user: string) => string
  me: string | undefined
}) {
  const people = summary?.contributors ?? []
  if (summary && people.length === 0) return null
  const shown = people.slice(0, 4)
  return (
    <RailSection
      id="knowledge-contributors"
      title="Contributors"
      meta={
        summary
          ? `${people.length} ${people.length === 1 ? "person" : "people"}`
          : undefined
      }
    >
      {summary ? (
        <>
          <div aria-hidden="true" className="mb-3.5 flex items-center gap-1">
            {people.slice(0, 6).map((person) => (
              <Person key={person.user} name={nameOf(person.user)} />
            ))}
            {people.length > 6 && (
              <span className="ml-1 text-2xs text-subtle tabular-nums">
                +{people.length - 6}
              </span>
            )}
          </div>
          <ul className="grid grid-cols-1 gap-2.5">
            {shown.map((person) => (
              <li
                key={person.user}
                className="flex items-baseline justify-between gap-3 text-xs"
              >
                <span className="truncate text-foreground">
                  {person.user === me ? "You" : nameOf(person.user)}
                </span>
                <span
                  className="shrink-0 text-2xs text-muted-foreground tabular-nums"
                  title={`Last upload ${formatDate(person.last_uploaded_at)}`}
                >
                  {formatCount(person.documents)}{" "}
                  {person.documents === 1 ? "document" : "documents"}
                </span>
              </li>
            ))}
          </ul>
        </>
      ) : (
        <Skeleton className="h-6 w-28 rounded-full" />
      )}
    </RailSection>
  )
}

/** The latest documents, as their uploads, newest first. */
const latest = (recent: KnowledgeDocument[]) =>
  [...recent]
    .sort((a, b) => b.created_at.localeCompare(a.created_at))
    .slice(0, 7)

const RUN_STATE: Record<
  IndexState,
  "running" | "completed" | "failed" | "planned"
> = {
  ready: "completed",
  indexing: "running",
  queued: "planned",
  failed: "failed",
  unknown: "planned",
}

function RecentActivity({
  recent,
  nameOf,
  me,
  onOpen,
}: {
  recent: KnowledgeDocument[] | undefined
  nameOf: (user: string) => string
  me: string | undefined
  onOpen: (document: KnowledgeDocument) => void
}) {
  const items = React.useMemo(() => latest(recent ?? []), [recent])
  if (recent && items.length === 0) return null
  return (
    <RailSection id="knowledge-activity" title="Recent activity">
      {recent ? (
        <ol className="grid grid-cols-1 gap-3.5">
          {items.map((document) => {
            const who =
              document.created_by === me ? "You" : nameOf(document.created_by)
            const state = stateOf(document.phase)
            return (
              <li
                key={document.id}
                className="flex min-w-0 items-start gap-2.5"
              >
                <Person name={nameOf(document.created_by)} />
                <div className="min-w-0 flex-1 text-xs">
                  <button
                    type="button"
                    className="block max-w-full truncate text-left text-foreground underline-offset-2 hover:underline"
                    title={`Open ${document.filename}`}
                    onClick={() => onOpen(document)}
                  >
                    {document.filename}
                  </button>
                  <p className="mt-0.5 flex min-w-0 items-center gap-1.5 text-2xs text-subtle">
                    <RunStateIcon state={RUN_STATE[state]} size={12} />
                    <span className="truncate">
                      {state === "ready" ? (
                        <span className="tabular-nums">
                          Indexed into {formatCount(document.chunk_count)}{" "}
                          {document.chunk_count === 1 ? "chunk" : "chunks"}
                        </span>
                      ) : state === "failed" ? (
                        <span className="text-danger-foreground">
                          Couldn't be indexed
                        </span>
                      ) : state === "unknown" ? (
                        "Not reported"
                      ) : (
                        INDEX_STATES[state].label
                      )}
                      {" · "}
                      {who === "You" ? "added by you" : `added by ${who}`}
                    </span>
                  </p>
                </div>
                <time
                  dateTime={document.created_at}
                  title={formatDate(document.created_at)}
                  className="shrink-0 pt-px text-2xs text-subtle"
                >
                  {formatWhen(document.created_at)}
                </time>
              </li>
            )
          })}
        </ol>
      ) : (
        <div className="grid gap-3" aria-busy="true">
          {[0, 1, 2].map((index) => (
            <Skeleton key={index} className="h-8 w-full" />
          ))}
        </div>
      )}
    </RailSection>
  )
}
