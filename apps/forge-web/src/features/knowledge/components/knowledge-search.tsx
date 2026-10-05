import * as React from "react"

import { PanelEmpty } from "@/components/forge/empty-state"
import { ErrorCallout } from "@/components/forge/feedback"
import { Icon } from "@/components/forge/icon"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Spinner } from "@/components/ui/spinner"
import {
  useSearchKnowledgeBase,
  type KnowledgeScope,
  type SearchHit,
} from "../lib/api"
import { KINDS, kindOf } from "../lib/knowledge"

/** How many chunks a test search asks for (the API takes 1–20). */
const SEARCH_LIMIT = 8

/**
 * Tries a search of the knowledge base as its agents would: a question in,
 * the chunks that answer it best out, each with its document, where in the
 * document it is (its headings) and how closely it matched. A hit opens its
 * document in the viewer.
 */
export function KnowledgeSearchDialog({
  open,
  onOpenChange,
  scope,
  baseName,
  onOpenDocument,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  scope: KnowledgeScope
  baseName: string
  /** Opens the hit's document in the viewer. */
  onOpenDocument: (documentId: string) => void
}) {
  const id = React.useId()
  const [query, setQuery] = React.useState("")
  const search = useSearchKnowledgeBase(scope)
  // What the shown hits answer, which the box may have moved on from.
  const [asked, setAsked] = React.useState<string>()

  const submit = (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    const question = query.trim()
    if (!question) return
    setAsked(question)
    search.mutate({ query: question, limit: SEARCH_LIMIT })
  }

  const hits = search.data?.hits
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="flex max-h-[min(44rem,calc(100dvh-2rem))] flex-col gap-5 sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>Test search</DialogTitle>
          <DialogDescription>
            Ask {baseName} what an agent would, and see the passages it gets
            back, best match first.
          </DialogDescription>
        </DialogHeader>
        <form onSubmit={submit} className="flex items-center gap-2">
          <label htmlFor={`${id}-query`} className="sr-only">
            Search query
          </label>
          <Input
            id={`${id}-query`}
            autoFocus
            autoComplete="off"
            placeholder="How do we rotate the payment keys?"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            className="flex-1"
          />
          <Button type="submit" disabled={!query.trim() || search.isPending}>
            {search.isPending ? (
              <Spinner data-icon="inline-start" />
            ) : (
              <Icon icon="search" data-icon="inline-start" />
            )}
            Search
          </Button>
        </form>

        <div
          tabIndex={0}
          role="region"
          aria-label="Search results"
          aria-busy={search.isPending}
          className="-mx-6 min-h-0 flex-1 overflow-y-auto px-6"
        >
          {search.error ? (
            <ErrorCallout title="Couldn't search the knowledge base">
              {search.error.message}
            </ErrorCallout>
          ) : hits === undefined ? (
            !search.isPending && (
              <PanelEmpty illustration="search">
                Results show here, with the document and section each passage
                comes from.
              </PanelEmpty>
            )
          ) : hits.length === 0 ? (
            <PanelEmpty illustration="search">
              Nothing in {baseName} matches “{asked}”. Only documents that are
              ready are searched.
            </PanelEmpty>
          ) : (
            <ol className="grid gap-2 pb-1">
              {hits.map((hit, index) => (
                <HitItem
                  key={hit.chunk_id}
                  hit={hit}
                  rank={index + 1}
                  onOpen={() => {
                    onOpenChange(false)
                    onOpenDocument(hit.document_id)
                  }}
                />
              ))}
            </ol>
          )}
        </div>
      </DialogContent>
    </Dialog>
  )
}

function HitItem({
  hit,
  rank,
  onOpen,
}: {
  hit: SearchHit
  rank: number
  onOpen: () => void
}) {
  const kind = KINDS[kindOf(hit.filename)]
  return (
    <li>
      <button
        type="button"
        onClick={onOpen}
        title={`Open ${hit.filename}`}
        className="grid w-full gap-1.5 rounded-(--radius-card) border px-3.5 py-3 text-left transition-[background-color] duration-150 hover:bg-muted/40"
      >
        <span className="flex min-w-0 items-center gap-2">
          <span className="w-4 shrink-0 text-2xs text-subtle tabular-nums">
            {rank}
          </span>
          <Icon
            icon={kind.icon}
            size={15}
            className="shrink-0 text-muted-foreground"
          />
          <span className="truncate text-[0.8125rem] font-medium text-foreground">
            {hit.filename}
          </span>
          <span
            className="ml-auto shrink-0 font-mono text-2xs text-muted-foreground tabular-nums"
            title="How closely it matched"
          >
            {hit.score.toFixed(3)}
          </span>
        </span>
        {hit.section_path.length > 0 && (
          <span className="truncate pl-6 text-2xs text-subtle">
            {hit.section_path.join(" › ")}
          </span>
        )}
        <span className="line-clamp-3 pl-6 text-xs/relaxed whitespace-pre-line text-muted-foreground">
          {hit.text}
        </span>
      </button>
    </li>
  )
}
