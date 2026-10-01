import { ViewOffSlashIcon } from "@hugeicons/core-free-icons"

import { Icon } from "@/components/forge/icon"
import { useShell } from "@/components/forge/shell"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { useFocusedEntity, type PageContext } from "@/lib/page-context"

/** What a chip calls a page: its title and the record in focus. */
const pageLabel = (title: string | undefined, focus: string | undefined) =>
  [title, focus].filter(Boolean).join(" · ")

/**
 * The chip itself: `label` names the page, and pressing it stops sharing
 * the page until it's pressed again. Without a page (`label` undefined) it
 * says so, and `emptyHint` explains how to get one.
 */
function PageContextToggle({
  label,
  sharing,
  onSharingChange,
  sharedHint,
  emptyHint,
}: {
  label: string | undefined
  sharing: boolean
  onSharingChange: (sharing: boolean) => void
  /** The tooltip while it's shared: what the assistant sees. */
  sharedHint: string
  emptyHint?: string
}) {
  const empty = label === undefined
  return (
    <div data-slot="page-context" className="flex min-w-0 px-1">
      <Tooltip>
        <TooltipTrigger
          render={
            <button
              type="button"
              aria-pressed={empty ? undefined : sharing}
              aria-disabled={empty || undefined}
              aria-label={
                empty
                  ? "No page to share with the assistant"
                  : `Share this page with the assistant: ${label}`
              }
              onClick={() => !empty && onSharingChange(!sharing)}
              className="inline-flex h-6 max-w-full min-w-0 items-center gap-1.5 rounded-full border border-border px-2 text-2xs text-muted-foreground transition-colors hover:border-foreground/25 hover:text-foreground aria-disabled:border-dashed aria-disabled:hover:border-border aria-disabled:hover:text-muted-foreground aria-[pressed=false]:border-dashed motion-reduce:transition-none"
            />
          }
        >
          <Icon
            icon={sharing && !empty ? "view" : ViewOffSlashIcon}
            size={13}
            className="shrink-0"
          />
          <span
            className={sharing || empty ? "truncate" : "truncate line-through"}
          >
            {empty ? "No Forge page open" : label}
          </span>
        </TooltipTrigger>
        <TooltipContent side="top">
          {empty
            ? emptyHint
            : sharing
              ? sharedHint
              : "Not shared. Click to share this page with your messages."}
        </TooltipContent>
      </Tooltip>
    </div>
  )
}

type SharingProps = {
  sharing: boolean
  onSharingChange: (sharing: boolean) => void
}

/**
 * At the top of the composer: the page the agent sees with each message,
 * and the record in focus. Pressing it stops sharing them until it's
 * pressed again.
 */
export function PageContextChip(props: SharingProps) {
  const title = useShell((shell) => shell.header.title)
  const focus = useFocusedEntity()
  return (
    <PageContextToggle
      {...props}
      label={pageLabel(title, focus?.label) || "This page"}
      sharedHint="The assistant sees this page with your messages. Click to stop sharing it."
    />
  )
}

/**
 * The chip in the assistant's own window: the page in the Forge tab the
 * person was in last (`page`, from `useLinkedPage`), or a note that there's
 * none open.
 */
export function LinkedPageChip({
  page,
  ...props
}: SharingProps & { page: PageContext | null }) {
  return (
    <PageContextToggle
      {...props}
      label={
        page ? pageLabel(page.title, page.focus?.label) || page.path : undefined
      }
      sharedHint="The assistant sees the page you're on in Forge with your messages, and follows you as you move around. Click to stop sharing it."
      emptyHint="Open Forge in a tab and the assistant sees the page you're on."
    />
  )
}
