import * as React from "react"
import { createPortal } from "react-dom"
import { Dialog as DialogPrimitive } from "@base-ui/react/dialog"
import { cn } from "cn"

import { Icon } from "@/components/forge/icon"
import type { IconProp } from "@/components/forge/icons"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogClose,
  DialogDescription,
  DialogOverlay,
  DialogPortal,
  DialogTitle,
} from "@/components/ui/dialog"
import { DIALOG_CARD, SETTINGS_POPUP, withViewTransition } from "./utils"
import {
  CardContext,
  CardStackContext,
  DEFAULT_COMPANION,
  MAX_COMPANIONS,
  useCardSpine,
  type CardInfo,
  type CardStack,
  type Spine,
} from "./settings-companion"
import "./settings-dialog.css"

/*
 * The settings dialog's look, shared by a step's settings (step-dialog.tsx)
 * and anything set up the same way (an MCP server): a card as tall as the
 * window over the dimmed page, with a header naming what's set up, sections
 * of settings that scroll, and a footer of actions. A setting can open a
 * card beside it (`SettingsCompanion`), and that card the next, up to four
 * cards in all (settings-companion.ts): the row stays centred, the cards
 * before glide left to make room, and the earlier ones fold into spines.
 */

/** The dimmed backdrop, fading with the card. */
export function SettingsOverlay() {
  return (
    <DialogOverlay className="bg-black/20 duration-150 supports-backdrop-filter:backdrop-blur-none dark:bg-black/45" />
  )
}

function useMediaQuery(query: string) {
  return React.useSyncExternalStore(
    (onChange) => {
      const list = window.matchMedia(query)
      list.addEventListener("change", onChange)
      return () => list.removeEventListener("change", onChange)
    },
    () => window.matchMedia(query).matches,
    () => false
  )
}

/**
 * A settings dialog. Its children are the settings card's: a
 * `SettingsHeader`, a scrolling body of `SettingsSection`s and a
 * `SettingsFooter`; any of them may open a `SettingsCompanion` beside it.
 */
export function SettingsDialog({
  open,
  onOpenChange,
  className,
  children,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  className?: string
  children: React.ReactNode
}) {
  const popup = React.useRef<HTMLDivElement>(null)
  const [slots, setSlots] = React.useState<(HTMLElement | null)[]>(() =>
    Array.from({ length: MAX_COMPANIONS }, () => null)
  )
  const [ids, setIds] = React.useState<string[]>([])
  const narrow = useMediaQuery("(max-width: 1079px)")
  const roomy = useMediaQuery("(min-width: 1700px)")
  const fullCards = narrow ? 1 : roomy ? 3 : 2
  // Closed, it opens again on its settings alone.
  if (!open && ids.length) setIds([])

  const slotRefs = React.useMemo(
    () =>
      Array.from(
        { length: MAX_COMPANIONS },
        (_, index) => (element: HTMLElement | null) =>
          setSlots((current) =>
            current[index] === element
              ? current
              : current.map((slot, i) => (i === index ? element : slot))
          )
      ),
    []
  )

  // The control that opened each card, to hand focus back to when it closes.
  const openers = React.useRef(new Map<string, HTMLElement | null>())
  const idsRef = React.useRef(ids)
  React.useLayoutEffect(() => {
    idsRef.current = ids
  }, [ids])

  const actions = React.useMemo(() => {
    const focus = (opener: HTMLElement | null | undefined) => {
      if (opener?.isConnected) opener.focus({ preventScroll: true })
      return document.activeElement === opener
    }
    return {
      open: (fromLevel: number, id: string, opener: HTMLElement | null) => {
        if (fromLevel >= MAX_COMPANIONS) return
        openers.current.set(id, opener)
        withViewTransition(() =>
          setIds((current) => [
            ...current.slice(0, fromLevel).filter((open) => open !== id),
            id,
          ])
        )
      },
      closeFrom: (level: number) => {
        // Focus goes back to what opened the first card closing before it
        // goes: a focused control vanishing would send focus to the dialog.
        // One in a folded card can't take it until the card has unfolded.
        const opener = openers.current.get(idsRef.current[level - 1])
        const moved = focus(opener)
        withViewTransition(
          () => setIds((current) => current.slice(0, Math.max(0, level - 1))),
          () => moved || focus(opener)
        )
      },
      release: (id: string) =>
        setIds((current) => {
          const index = current.indexOf(id)
          return index === -1 ? current : current.slice(0, index)
        }),
      reveal: (next: string[]) => {
        const shown = next.slice(0, MAX_COMPANIONS)
        const same = (current: string[]) =>
          current.length === shown.length &&
          current.every((id, index) => id === shown[index])
        if (same(idsRef.current)) return
        // What opened them isn't known: closing, focus stays in the dialog.
        for (const id of shown) openers.current.delete(id)
        withViewTransition(() =>
          setIds((current) => (same(current) ? current : shown))
        )
      },
    }
  }, [])

  const stack = React.useMemo<CardStack>(
    () => ({ slots, ids, fullCards, ...actions }),
    [slots, ids, fullCards, actions]
  )

  return (
    <Dialog
      open={open}
      onOpenChange={(next, details) => {
        if (next) return onOpenChange(true)
        const target = details.event?.target
        // Completions and hovers float in the page's body, outside the dialog:
        // picking one isn't leaving it.
        if (
          details.reason === "outside-press" &&
          target instanceof Element &&
          target.closest(".cm-tooltip")
        ) {
          details.cancel()
          return
        }
        // Escape that closed a field's completions was the field's.
        if (details.reason === "escape-key" && details.event.defaultPrevented) {
          details.cancel()
          return
        }
        // With cards open beside the settings, Escape or a click outside
        // closes the deepest first, unsaved, and the rest stay.
        if (
          ids.length &&
          (details.reason === "escape-key" || details.reason === "outside-press")
        ) {
          details.cancel()
          actions.closeFrom(ids.length)
          return
        }
        onOpenChange(false)
      }}
    >
      <DialogPortal>
        {/* It fades with the cards: shorter, it would be done (and gone) before them. */}
        <SettingsOverlay />
        {/* The cards sit side by side, centred as a row; around them is the backdrop. */}
        <DialogPrimitive.Popup
          ref={popup}
          initialFocus={popup}
          data-slot="dialog-content"
          className={SETTINGS_POPUP}
        >
          <CardStackContext.Provider value={stack}>
            <SettingsCard
              level={0}
              className={cn("w-[clamp(34rem,34vw,52rem)]", className)}
            >
              {children}
            </SettingsCard>
            {slotRefs.map((ref, index) => (
              <div key={index} ref={ref} className="contents" />
            ))}
          </CardStackContext.Provider>
        </DialogPrimitive.Popup>
      </DialogPortal>
    </Dialog>
  )
}

/** An element a card renders as (a form), given the card's props. */
type CardElement = React.ReactElement<
  React.HTMLAttributes<HTMLElement> & { ref?: React.Ref<HTMLElement> }
>

/**
 * One card in the row, at `level` (0 = the settings). Shows in full while
 * it's among the deepest `fullCards`; otherwise folds to a spine (its
 * content stays mounted, so nothing typed in it is lost) or, under 1080px,
 * hides behind the deepest card.
 */
function SettingsCard({
  level,
  render,
  titleId,
  descriptionId,
  className,
  children,
}: {
  level: number
  render?: CardElement
  titleId?: string
  descriptionId?: string
  className?: string
  children: React.ReactNode
}) {
  const stack = React.useContext(CardStackContext)!
  const [spine, setSpine] = React.useState<Spine>({ title: "" })
  const info = React.useMemo<CardInfo>(() => ({ level, setSpine }), [level])
  const cards = stack.ids.length + 1
  const fromEnd = cards - 1 - level
  const folded = fromEnd >= stack.fullCards
  const deepest = fromEnd === 0
  // Under 1080px the deeper cards cover the row, and only the deepest shows.
  const covering = stack.fullCards === 1 && level > 0
  const hidden = stack.fullCards === 1 && !deepest

  // Opened, focus goes into it (unless something in it took it already).
  const panel = React.useRef<HTMLElement | null>(null)
  React.useEffect(() => {
    const element = panel.current
    if (level > 0 && element && !element.contains(document.activeElement))
      element.focus()
  }, [level])

  const props = {
    ref: (element: HTMLElement | null) => {
      panel.current = element
    },
    tabIndex: level > 0 ? -1 : undefined,
    role: level > 0 ? "region" : undefined,
    "aria-labelledby": titleId,
    "aria-describedby": descriptionId,
    "data-level": level,
    "data-folded": folded || undefined,
    // A name per level lets the browser glide each card to its new place.
    style: { viewTransitionName: `settings-card-${level}` },
    className: cn(
      DIALOG_CARD,
      "relative",
      folded
        ? "w-12 min-w-12 shrink-0"
        : level === 0
          ? cards > 1
            ? "max-w-full min-w-[28rem] shrink"
            : "max-w-full shrink-0"
          : "shrink",
      hidden && "invisible",
      covering &&
        "max-[1079px]:absolute max-[1079px]:inset-0 max-[1079px]:mx-auto max-[1079px]:w-auto max-[1079px]:max-w-[47.5rem] max-[1079px]:min-w-0",
      render?.props.className,
      folded ? undefined : className
    ),
    children: (
      <CardContext.Provider value={info}>
        {folded && (
          <button
            type="button"
            onClick={() => stack.closeFrom(level + 1)}
            aria-label={
              typeof spine.title === "string" ? spine.title : undefined
            }
            className="flex h-full w-full flex-col items-center gap-3 py-4 text-muted-foreground transition-colors outline-none hover:bg-accent hover:text-foreground focus-visible:bg-accent focus-visible:text-foreground"
          >
            {spine.glyph ?? <Icon icon="left" size={16} />}
            <span className="rotate-180 truncate text-xs font-medium [writing-mode:vertical-rl]">
              {spine.title}
            </span>
          </button>
        )}
        {/* Folded, it keeps its content mounted (and its state) but out of view. */}
        <div className={cn("flex min-h-0 flex-1 flex-col", folded && "hidden")}>
          {children}
        </div>
      </CardContext.Provider>
    ),
  }

  return render ? React.cloneElement(render, props) : <section {...props} />
}

/**
 * A glyph tile, as a step's header shows its kind: plain, or tinted as the
 * builders tint tools (`action`).
 */
export function SettingsGlyph({
  icon,
  tone,
}: {
  icon: IconProp
  tone?: "action"
}) {
  return (
    <span
      aria-hidden="true"
      className={cn(
        "grid size-7 shrink-0 place-items-center rounded-(--radius-soft) border",
        tone === "action"
          ? "border-transparent bg-kind-action text-kind-action-foreground"
          : "bg-background text-muted-foreground"
      )}
    >
      <Icon icon={icon} size={15} />
    </span>
  )
}

/**
 * What's set up: its glyph, name, one line about it, and closing. Its
 * title and glyph also label the settings' spine, when cards opened beside
 * them fold it.
 */
export function SettingsHeader({
  glyph,
  title,
  description,
  closeLabel,
}: {
  glyph: React.ReactNode
  title: React.ReactNode
  description?: React.ReactNode
  /** The close button's name for screen readers. */
  closeLabel: string
}) {
  useCardSpine(title, glyph)
  return (
    <header className="flex items-start gap-3 border-b px-5 pt-4 pb-3.5">
      {glyph}
      <div className="flex min-w-0 flex-1 flex-col gap-0.5">
        <DialogTitle className="truncate text-sm leading-snug font-medium text-foreground">
          {title}
        </DialogTitle>
        {description && (
          <DialogDescription className="truncate text-2xs text-muted-foreground">
            {description}
          </DialogDescription>
        )}
      </div>
      <DialogClose
        render={
          <Button
            variant="ghost"
            size="icon-sm"
            aria-label={closeLabel}
            className="-mt-0.5 -mr-1.5 text-muted-foreground"
          />
        }
      >
        <Icon icon="close" />
      </DialogClose>
    </header>
  )
}

/** The settings' scrolling body. */
export function SettingsBody({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      className={cn("min-h-0 flex-1 overflow-y-auto overscroll-contain", className)}
      {...props}
    />
  )
}

/** A group of settings, ruled off from the one above. */
export function SettingsSection({
  title,
  className,
  children,
}: {
  title?: string
  className?: string
  children: React.ReactNode
}) {
  return (
    <section
      className={cn(
        "flex flex-col gap-4 border-t px-5 py-4 first:border-t-0",
        className
      )}
    >
      {title && (
        <h3 className="-mb-1 text-xs font-medium text-foreground">{title}</h3>
      )}
      {children}
    </section>
  )
}

/** The actions along the bottom; a `SettingsHint` pushes what follows it right. */
export function SettingsFooter({ children }: { children: React.ReactNode }) {
  return (
    <footer className="flex items-center gap-2 border-t px-5 py-3">
      {children}
    </footer>
  )
}

/** A quiet note in the footer, before the closing actions. */
export function SettingsHint({ children }: { children: React.ReactNode }) {
  return (
    <span className="ml-auto text-2xs text-subtle max-[600px]:hidden">
      {children}
    </span>
  )
}

/** A card's width beside others: it shrinks to its least as more open. */
const COMPANION_SIZES = {
  default: "w-[47.5rem] min-w-[28rem]",
  sheet: "w-[35rem] min-w-[26rem]",
  narrow: "w-[30rem] min-w-[22rem]",
}

/**
 * A card beside the one its opener is in, while `useCompanionCard(id)` (or
 * `useSettingsCompanion(id)`) has it open: a larger editor for a setting,
 * with a header, a scrolling body and a footer of its own. Pass
 * `render={<form onSubmit={…} />}` to make it a form. What's in it can open
 * the next card, up to four in all. Declare it anywhere inside the dialog:
 * where it shows is decided by the order cards were opened in, not where
 * it's written. Gone while open (what opened it went away), it closes too.
 */
export function SettingsCompanion({
  id = DEFAULT_COMPANION,
  title,
  description,
  icon,
  footer,
  size = "default",
  render,
  closeLabel = "Close this panel",
  bodyClassName,
  children,
}: {
  id?: string
  title: React.ReactNode
  description?: React.ReactNode
  /** Its spine's icon, when it's folded. */
  icon?: IconProp
  footer?: React.ReactNode
  /** `default` 47.5rem, `sheet` 35rem, `narrow` 30rem; each shrinks beside others. */
  size?: keyof typeof COMPANION_SIZES
  render?: CardElement
  /** The close button's name for screen readers. */
  closeLabel?: string
  bodyClassName?: string
  children: React.ReactNode
}) {
  const stack = React.useContext(CardStackContext)
  const titleId = React.useId()
  const descriptionId = React.useId()
  const index = stack ? stack.ids.indexOf(id) : -1

  // Gone, it closes; but not when it's put straight back (React's strict
  // mode does that to a card that opens as it mounts, as `reveal` opens one).
  const release = stack?.release
  const mounted = React.useRef<string | null>(null)
  React.useEffect(() => {
    mounted.current = id
    return () => {
      mounted.current = null
      queueMicrotask(() => {
        if (mounted.current !== id) release?.(id)
      })
    }
  }, [release, id])

  const slot = index === -1 ? null : stack?.slots[index]
  if (!slot) return null
  return createPortal(
    <SettingsCard
      level={index + 1}
      render={render}
      titleId={titleId}
      descriptionId={description ? descriptionId : undefined}
      className={COMPANION_SIZES[size]}
    >
      <CompanionHeader
        title={title}
        titleId={titleId}
        description={description}
        descriptionId={descriptionId}
        icon={icon}
        closeLabel={closeLabel}
      />
      <SettingsBody className={bodyClassName}>{children}</SettingsBody>
      {footer && <SettingsFooter>{footer}</SettingsFooter>}
    </SettingsCard>,
    slot
  )
}

/**
 * A card's header beside the settings. Its ✕ closes it and anything deeper,
 * unsaved; where only the deepest card shows, that's going back to the one
 * before, so it reads as Back.
 */
function CompanionHeader({
  title,
  titleId,
  description,
  descriptionId,
  icon,
  closeLabel,
}: {
  title: React.ReactNode
  titleId: string
  description?: React.ReactNode
  descriptionId: string
  icon?: IconProp
  closeLabel: string
}) {
  const stack = React.useContext(CardStackContext)
  const { level } = React.useContext(CardContext)
  const glyph = React.useMemo(
    () => icon && <Icon icon={icon} size={16} />,
    [icon]
  )
  useCardSpine(title, glyph)
  const back = stack?.fullCards === 1
  return (
    <header className="flex items-start gap-3 border-b px-5 pt-4 pb-3.5">
      <div className="flex min-w-0 flex-1 flex-col gap-0.5">
        <h2
          id={titleId}
          className="truncate text-sm leading-snug font-medium text-foreground"
        >
          {title}
        </h2>
        {description && (
          <p
            id={descriptionId}
            className="max-w-[36rem] text-xs/[1.6] text-muted-foreground"
          >
            {description}
          </p>
        )}
      </div>
      <Button
        type="button"
        variant="ghost"
        size="icon-sm"
        aria-label={back ? "Back" : closeLabel}
        className="-mt-0.5 -mr-1.5 text-muted-foreground"
        onClick={() => stack?.closeFrom(level)}
      >
        <Icon icon={back ? "left" : "close"} />
      </Button>
    </header>
  )
}
