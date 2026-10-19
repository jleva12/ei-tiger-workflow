import * as React from "react"
import type { SuggestionProps } from "@tiptap/suggestion"
import { cn } from "cn"

import { Spinner } from "@/components/ui/spinner"

/** How one kind of suggestion (commands, mentions, emoji) lists its items. */
export interface SuggestionView<I> {
  /** Names the list for screen readers. */
  label: string
  /** Shown when the query matches nothing. */
  empty: string
  getKey: (item: I) => string
  getGroup?: (item: I) => string | undefined
  renderItem: (item: I) => React.ReactNode
}

export interface SuggestionMenuHandle {
  onKeyDown: (event: KeyboardEvent) => boolean
}

export type SuggestionMenuProps = SuggestionProps<unknown, unknown> & {
  view: SuggestionView<unknown>
  ref?: React.Ref<SuggestionMenuHandle>
}

/**
 * The popup under `/`, `@` and `:`. Focus stays in the editor, so the arrow
 * keys, Enter and Tab reach it through `onKeyDown`; the pointer works too.
 */
export function SuggestionMenu({
  items,
  command,
  query,
  loading,
  view,
  ref,
}: SuggestionMenuProps) {
  const [active, setActive] = React.useState(0)
  const listRef = React.useRef<HTMLDivElement>(null)
  const id = React.useId()

  // A new query starts at the top of the new results.
  const [shownItems, setShownItems] = React.useState(items)
  if (shownItems !== items) {
    setShownItems(items)
    setActive(0)
  }

  React.useEffect(() => {
    listRef.current
      ?.querySelector(`[data-index="${active}"]`)
      ?.scrollIntoView({ block: "nearest" })
  }, [active])

  const select = (index: number) => {
    const item = items[index]
    if (item !== undefined) command(item)
  }

  React.useImperativeHandle(ref, () => ({
    onKeyDown: (event) => {
      if (!items.length) return false
      if (event.key === "ArrowDown") {
        setActive((index) => (index + 1) % items.length)
        return true
      }
      if (event.key === "ArrowUp") {
        setActive((index) => (index - 1 + items.length) % items.length)
        return true
      }
      if (event.key === "Enter" || event.key === "Tab") {
        select(active)
        return true
      }
      return false
    },
  }))

  let previousGroup: string | undefined
  return (
    <div
      ref={listRef}
      role="listbox"
      aria-label={view.label}
      data-slot="rich-text-suggestions"
      className="max-h-80 w-64 overflow-y-auto overscroll-contain rounded-(--radius-band) border border-border bg-popover p-1 text-popover-foreground shadow-(--shadow-float)"
    >
      {loading && !items.length ? (
        <div className="flex items-center gap-2 px-2 py-1.5 text-xs text-muted-foreground">
          <Spinner className="size-3.5" />
          Searching…
        </div>
      ) : !items.length ? (
        <div className="px-2 py-1.5 text-xs text-muted-foreground">
          {view.empty}
        </div>
      ) : (
        items.map((item, index) => {
          // Groups while browsing; a query ranks across them instead.
          const group = query ? undefined : view.getGroup?.(item)
          const heading = group !== previousGroup ? group : undefined
          previousGroup = group
          return (
            <React.Fragment key={view.getKey(item)}>
              {heading && (
                <div
                  role="presentation"
                  className="px-2 pt-2 pb-1 text-2xs text-muted-foreground first:pt-1"
                >
                  {heading}
                </div>
              )}
              <div
                id={`${id}-${index}`}
                role="option"
                aria-selected={index === active}
                data-index={index}
                data-active={index === active || undefined}
                // Keep the caret in the editor.
                onMouseDown={(event) => event.preventDefault()}
                onMouseMove={() => index !== active && setActive(index)}
                onClick={() => select(index)}
                className={cn(
                  "flex min-h-8 cursor-default items-center gap-2 rounded-(--radius-item) px-2 py-1.5 text-xs select-none data-active:bg-accent data-active:text-accent-foreground",
                  "[&_svg]:shrink-0 [&_svg:not([class*='size-'])]:size-4"
                )}
              >
                {view.renderItem(item)}
              </div>
            </React.Fragment>
          )
        })
      )}
    </div>
  )
}
