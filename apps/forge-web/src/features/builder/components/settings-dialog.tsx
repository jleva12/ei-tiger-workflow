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
import { DIALOG_CARD, SETTINGS_CARD, SETTINGS_POPUP, withViewTransition } from "./utils"
import {
  CompanionSlotContext,
  type CompanionSlot,
} from "./settings-companion"
import "./step-dialog.css"

/*
 * The settings dialog's look, shared by a step's settings (step-dialog.tsx)
 * and anything set up the same way (an MCP server): a card as tall as the
 * window over the dimmed page, with a header naming what's set up, sections
 * of settings that scroll, and a footer of actions. A setting can open a
 * second card beside it (`SettingsCompanion`), and the pair stays centred:
 * the settings glide left to make room.
 */


/** The dimmed backdrop, fading with the card. */
export function SettingsOverlay() {
  return (
    <DialogOverlay className="bg-black/20 duration-150 supports-backdrop-filter:backdrop-blur-none dark:bg-black/45" />
  )
}

/**
 * A settings dialog. Its children are the settings card's: a
 * `SettingsHeader`, a scrolling body of `SettingsSection`s and a
 * `SettingsFooter`; one of them may open a `SettingsCompanion` beside it.
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
  const [slot, setSlot] = React.useState<HTMLDivElement | null>(null)
  const [companion, setCompanion] = React.useState(false)
  // Closed, it opens again without its companion.
  if (!open && companion) setCompanion(false)
  const context = React.useMemo<CompanionSlot>(
    () => ({ slot, open: companion, setOpen: setCompanion }),
    [slot, companion]
  )
  return (
    <Dialog
      open={open}
      onOpenChange={(next, details) => {
        // With a card open beside the settings, Escape or a click outside
        // closes that card first, and the settings stay.
        if (
          !next &&
          companion &&
          (details.reason === "escape-key" || details.reason === "outside-press")
        ) {
          details.cancel()
          withViewTransition(() => setCompanion(false))
          return
        }
        onOpenChange(next)
      }}
    >
      <DialogPortal>
        <SettingsOverlay />
        <DialogPrimitive.Popup
          ref={popup}
          initialFocus={popup}
          data-slot="dialog-content"
          className={SETTINGS_POPUP}
        >
          <CompanionSlotContext.Provider value={context}>
            <div
              className={cn(
                SETTINGS_CARD,
                "[view-transition-name:step-settings]",
                // Too narrow for two: the card beside covers the settings.
                companion && "max-[1080px]:invisible",
                className
              )}
            >
              {children}
            </div>
            <div ref={setSlot} className="contents" />
          </CompanionSlotContext.Provider>
        </DialogPrimitive.Popup>
      </DialogPortal>
    </Dialog>
  )
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

/** What's set up: its glyph, name, one line about it, and closing. */
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

/**
 * The card beside the settings, while `useSettingsCompanion` has it open:
 * a larger editor for one of them, with a header, a scrolling body and a
 * footer of its own.
 */
export function SettingsCompanion({
  title,
  description,
  footer,
  children,
}: {
  title: React.ReactNode
  description?: React.ReactNode
  footer?: React.ReactNode
  children: React.ReactNode
}) {
  const context = React.useContext(CompanionSlotContext)
  const titleId = React.useId()
  if (!context?.open || !context.slot) return null
  return createPortal(
    <section
      aria-labelledby={titleId}
      className={cn(
        DIALOG_CARD,
        "w-[clamp(28rem,30vw,40rem)] min-w-[24rem] shrink [view-transition-name:step-companion]",
        // Too narrow for two: it covers the settings.
        "max-[1080px]:absolute max-[1080px]:inset-0 max-[1080px]:w-auto max-[1080px]:min-w-0"
      )}
    >
      <header className="flex items-start gap-3 border-b px-5 pt-4 pb-3.5">
        <div className="flex min-w-0 flex-1 flex-col gap-0.5">
          <h2
            id={titleId}
            className="truncate text-sm leading-snug font-medium text-foreground"
          >
            {title}
          </h2>
          {description && (
            <p className="text-xs/[1.6] text-muted-foreground">{description}</p>
          )}
        </div>
        <Button
          type="button"
          variant="ghost"
          size="icon-sm"
          aria-label="Close this panel"
          className="-mt-0.5 -mr-1.5 text-muted-foreground"
          onClick={() => withViewTransition(() => context.setOpen(false))}
        >
          <Icon icon="close" />
        </Button>
      </header>
      <SettingsBody>{children}</SettingsBody>
      {footer && <SettingsFooter>{footer}</SettingsFooter>}
    </section>,
    context.slot
  )
}
