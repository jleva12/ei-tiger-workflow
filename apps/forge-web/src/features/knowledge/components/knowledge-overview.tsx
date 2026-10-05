import * as React from "react"
import { Link } from "@tanstack/react-router"
import {
  Delete02Icon,
  FileEmpty02Icon,
  Folder01Icon,
  FolderAddIcon,
  PencilEdit02Icon,
} from "@hugeicons/core-free-icons"
import { cn } from "cn"

import { Icon } from "@/components/forge/icon"
import { Button } from "@/components/ui/button"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { Skeleton } from "@/components/ui/skeleton"
import type {
  DocumentCollection,
  DocumentSummary,
  KnowledgeScope,
} from "../lib/api"
import {
  formatBytes,
  formatCount,
  KIND_KEYS,
  KINDS,
  totalsByKind,
  UNFILED,
  type KindKey,
} from "../lib/knowledge"

const plural = (count: number, one: string, many = `${one}s`) =>
  `${formatCount(count)} ${count === 1 ? one : many}`

/**
 * The knowledge base's documents by kind, one hairline-divided band of filter cells:
 * each shows how many there are and their size, and answers the page's
 * question for its kind: how many agents can read, drawn as a thin
 * ready / failed bar along the cell. Pressing one filters the list to that
 * kind; pressing it again clears it.
 */
export function KindStrip({
  summary,
  kind,
  onKindChange,
}: {
  summary: DocumentSummary | undefined
  kind: KindKey | undefined
  onKindChange: (kind: KindKey | undefined) => void
}) {
  const totals = totalsByKind(summary)
  const bytes = summary?.totals.size_bytes ?? 0
  return (
    <div>
      <div
        role="group"
        aria-label="Filter by kind"
        className="grid grid-cols-7 gap-px overflow-hidden rounded-(--radius-band) border bg-border @max-[1700px]/shell:grid-cols-4 @max-[600px]/shell:grid-cols-2"
      >
        {KIND_KEYS.map((key) => {
          const { label, icon } = KINDS[key]
          const sum = totals[key]
          const share = bytes ? Math.round((sum.size_bytes / bytes) * 100) : 0
          const pressed = kind === key
          const readiness =
            key === "other" && sum.documents > 0
              ? { text: "Not readable", tone: "danger" }
              : sum.failed > 0
                ? { text: `${formatCount(sum.failed)} failed`, tone: "danger" }
                : sum.documents > 0 && sum.ready === sum.documents
                  ? { text: "All ready", tone: "quiet" }
                  : sum.documents > 0
                    ? { text: `${formatCount(sum.ready)} ready`, tone: "quiet" }
                    : undefined
          return (
            <button
              key={key}
              type="button"
              aria-pressed={pressed}
              onClick={() => onKindChange(pressed ? undefined : key)}
              title={`${label}: ${share}% of the stored bytes`}
              className="group/kind relative flex min-w-0 flex-col items-start gap-3 bg-background px-4 pt-3.5 pb-3.5 text-left transition-[background-color] duration-150 hover:bg-[color-mix(in_oklch,var(--muted)_50%,var(--background))] aria-pressed:bg-muted @max-[1700px]/shell:last:col-span-2 @max-[800px]/shell:px-3 @max-[600px]/shell:py-2.5"
            >
              <span className="flex size-7 shrink-0 items-center justify-center rounded-(--radius-soft) bg-muted text-foreground transition-[background-color] duration-150 group-hover/kind:bg-background group-aria-pressed/kind:bg-background @max-[600px]/shell:hidden">
                <Icon icon={icon} size={16} />
              </span>
              <span className="grid w-full min-w-0 gap-0.5">
                <span className="truncate text-[0.8125rem] font-medium text-foreground">
                  {label}
                </span>
                {summary ? (
                  <span className="flex items-baseline justify-between gap-2 text-xs text-muted-foreground tabular-nums">
                    <span className="truncate">
                      {formatCount(sum.documents)}
                      {sum.documents > 0 && ` · ${formatBytes(sum.size_bytes)}`}
                    </span>
                    {readiness && (
                      <span
                        className={cn(
                          "shrink-0 text-2xs",
                          readiness.tone === "danger"
                            ? "text-danger-foreground"
                            : "text-subtle"
                        )}
                      >
                        {readiness.text}
                      </span>
                    )}
                  </span>
                ) : (
                  <Skeleton className="mt-1 h-3 w-20" />
                )}
              </span>
              {summary && sum.documents > 0 && (
                <ReadinessLine
                  ready={key === "other" ? 0 : sum.ready}
                  failed={key === "other" ? sum.documents : sum.failed}
                  total={sum.documents}
                />
              )}
            </button>
          )
        })}
      </div>
    </div>
  )
}

/** A hairline along a kind cell's foot: its ready share, then its failed. */
function ReadinessLine({
  ready,
  failed,
  total,
}: {
  ready: number
  failed: number
  total: number
}) {
  return (
    <span
      aria-hidden="true"
      className="absolute inset-x-4 bottom-0 flex h-0.5 overflow-hidden rounded-t-full bg-muted @max-[800px]/shell:inset-x-3"
    >
      <span
        className="h-full bg-status-success-foreground transition-[width] duration-300 ease-out motion-reduce:transition-none"
        style={{ width: `${(ready / total) * 100}%` }}
      />
      <span
        className="h-full bg-status-failed-foreground transition-[width] duration-300 ease-out motion-reduce:transition-none"
        style={{ width: `${(failed / total) * 100}%` }}
      />
    </span>
  )
}

/**
 * The collections at this level as tiles, like a drive's folders: the top
 * ones, with Unfiled last when anything is, or those inside the open one.
 * A tile opens its collection; a manager's tiles have a menu to rename or
 * delete it.
 */
export function CollectionShelf({
  scope,
  collections,
  totals,
  unfiled,
  inside,
  selected,
  canManage,
  onNew,
  onRename,
  onDelete,
}: {
  scope: KnowledgeScope
  /** The collections at this level. */
  collections: DocumentCollection[] | undefined
  /** A collection's documents and bytes, those in it included. */
  totals: (id: string) => { documents: number; size_bytes: number }
  unfiled: number
  /** The open collection; the top of the knowledge base when undefined. */
  inside: DocumentCollection | undefined
  /** The collection open, or `UNFILED`. */
  selected: string | undefined
  canManage: boolean
  onNew: () => void
  onRename: (collection: DocumentCollection) => void
  onDelete: (collection: DocumentCollection) => void
}) {
  // Inside a collection, only when it holds others; the top offers to start.
  if (collections?.length === 0 && (!canManage || inside || selected))
    return null
  return (
    <section aria-labelledby="knowledge-collections" className="mt-8">
      <div className="mb-3 flex min-h-7 items-center justify-between gap-3">
        <h2
          id="knowledge-collections"
          className="text-[0.8125rem] font-medium text-muted-foreground"
        >
          {inside ? `Collections in ${inside.name}` : "Collections"}
        </h2>
        {canManage && collections !== undefined && collections.length > 0 && (
          <Button variant="ghost" size="xs" onClick={onNew}>
            <Icon icon={FolderAddIcon} data-icon="inline-start" />
            New collection
          </Button>
        )}
      </div>
      {collections === undefined ? (
        <div className="grid grid-cols-5 gap-3 @max-[800px]/shell:grid-cols-3 @max-[600px]/shell:-mx-[13px] @max-[600px]/shell:flex @max-[600px]/shell:gap-2 @max-[600px]/shell:overflow-x-auto @max-[600px]/shell:px-[13px] @max-[600px]/shell:pb-1">
          {[0, 1, 2].map((key) => (
            <Skeleton
              key={key}
              className="h-[92px] rounded-(--radius-card) @max-[600px]/shell:h-12 @max-[600px]/shell:w-40 @max-[600px]/shell:shrink-0"
            />
          ))}
        </div>
      ) : collections.length === 0 ? (
        <button
          type="button"
          onClick={onNew}
          className="flex w-full items-center gap-3.5 rounded-(--radius-card) border border-dashed px-4 py-3.5 text-left transition-[background-color] duration-150 hover:bg-muted/50"
        >
          <span className="flex size-7 shrink-0 items-center justify-center rounded-(--radius-soft) bg-muted text-foreground">
            <Icon icon={FolderAddIcon} size={16} />
          </span>
          <span className="grid gap-0.5">
            <span className="text-[0.8125rem] font-medium text-foreground">
              New collection
            </span>
            <span className="text-xs text-muted-foreground">
              Group documents so people find them faster. Agents search every
              document either way.
            </span>
          </span>
        </button>
      ) : (
        <ul className="grid grid-cols-5 gap-3 @max-[800px]/shell:grid-cols-3 @max-[600px]/shell:-mx-[13px] @max-[600px]/shell:flex @max-[600px]/shell:gap-2 @max-[600px]/shell:overflow-x-auto @max-[600px]/shell:px-[13px] @max-[600px]/shell:pb-1">
          {collections.map((collection) => (
            <CollectionTile
              key={collection.id}
              scope={scope}
              id={collection.id}
              name={collection.name}
              icon={Folder01Icon}
              detail={(() => {
                const sum = totals(collection.id)
                return sum.documents
                  ? `${plural(sum.documents, "document")} · ${formatBytes(sum.size_bytes)}`
                  : "Empty"
              })()}
              description={collection.description}
              active={selected === collection.id}
              menu={
                canManage ? (
                  <DropdownMenu>
                    <DropdownMenuTrigger
                      render={
                        <Button
                          variant="ghost"
                          size="icon-xs"
                          aria-label={`Actions for ${collection.name}`}
                          className="relative z-10 -mt-1 -mr-1.5"
                        />
                      }
                    >
                      <Icon icon="more" />
                    </DropdownMenuTrigger>
                    <DropdownMenuContent align="end" className="min-w-40">
                      <DropdownMenuItem onClick={() => onRename(collection)}>
                        <Icon icon={PencilEdit02Icon} />
                        Rename…
                      </DropdownMenuItem>
                      <DropdownMenuSeparator />
                      <DropdownMenuItem
                        variant="destructive"
                        onClick={() => onDelete(collection)}
                      >
                        <Icon icon={Delete02Icon} />
                        Delete…
                      </DropdownMenuItem>
                    </DropdownMenuContent>
                  </DropdownMenu>
                ) : undefined
              }
            />
          ))}
          {unfiled > 0 && !inside && (
            <CollectionTile
              scope={scope}
              id={UNFILED}
              name="Unfiled"
              icon={FileEmpty02Icon}
              detail={plural(unfiled, "document")}
              description="In no collection"
              active={selected === UNFILED}
            />
          )}
        </ul>
      )}
    </section>
  )
}

function CollectionTile({
  scope,
  id,
  name,
  icon,
  detail,
  description,
  active,
  menu,
}: {
  scope: KnowledgeScope
  id: string
  name: string
  icon: React.ComponentProps<typeof Icon>["icon"]
  detail: string
  description?: string
  active: boolean
  menu?: React.ReactNode
}) {
  return (
    <li
      className={cn(
        "relative flex min-w-0 flex-col gap-3 rounded-(--radius-card) border px-3.5 pt-3.5 pb-3 transition-[background-color,border-color] duration-150 has-[a:hover]:bg-muted/40 @max-[600px]/shell:w-52 @max-[600px]/shell:shrink-0 @max-[600px]/shell:flex-row-reverse @max-[600px]/shell:items-center @max-[600px]/shell:gap-2.5 @max-[600px]/shell:py-2",
        active && "border-subtle/50 bg-muted has-[a:hover]:bg-muted"
      )}
    >
      <div className="flex items-start justify-between gap-2 @max-[600px]/shell:contents">
        <span
          className={cn(
            "flex size-7 shrink-0 items-center justify-center rounded-(--radius-soft) bg-muted text-foreground @max-[600px]/shell:order-last",
            active && "bg-background"
          )}
        >
          <Icon icon={icon} size={16} />
        </span>
        {menu}
      </div>
      <div className="grid min-w-0 flex-1 gap-0.5">
        <Link
          to="/organizations/$organizationId/knowledge/$knowledgeBaseId"
          params={scope}
          search={(prev) => ({
            ...prev,
            shelf: undefined,
            collection: active ? undefined : id,
          })}
          aria-current={active ? "page" : undefined}
          title={description || name}
          className="truncate text-[0.8125rem] font-medium text-foreground outline-offset-4 after:absolute after:inset-0 after:rounded-(--radius-card)"
        >
          {name}
        </Link>
        <span className="truncate text-xs text-muted-foreground tabular-nums">
          {detail}
        </span>
      </div>
    </li>
  )
}
