"use client"

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react"
import { cva, type VariantProps } from "class-variance-authority"
import {
  BrainIcon,
  ChevronDownIcon,
} from "@/components/assistant-ui/elements/aui-icons"
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible"
import { cn } from "cn"

export const ANIMATION_DURATION = 200

const ReasoningPreviewContext = createContext(false)

const reasoningVariants = cva("aui-reasoning-root my-5 w-full", {
  variants: {
    variant: {
      outline: "rounded-(--radius-card) border px-3 py-2",
      ghost: "",
      muted: "rounded-(--radius-card) bg-muted/50 px-3 py-2",
    },
  },
  defaultVariants: {
    variant: "ghost",
  },
})

/** "12 seconds", "1 second", "1m 5s". */
const formatSeconds = (seconds: number) =>
  seconds < 60
    ? `${seconds} ${seconds === 1 ? "second" : "seconds"}`
    : `${Math.floor(seconds / 60)}m ${seconds % 60}s`

export type ReasoningRootProps = Omit<
  React.ComponentProps<typeof Collapsible>,
  "open" | "onOpenChange"
> &
  VariantProps<typeof reasoningVariants> & {
    open?: boolean
    onOpenChange?: (open: boolean) => void
    defaultOpen?: boolean
    /**
     * Whether the reasoning is currently streaming. While the disclosure is
     * open during streaming it shows a bottom-pinned live preview that keeps
     * following the newest tokens, and pauses while the reader is scrolled
     * up.
     */
    streaming?: boolean
    /**
     * Opens the disclosure by itself while `streaming`, and closes it when
     * streaming ends (back to `defaultOpen`), until the first manual toggle
     * takes over. Off by default: in a thread, a panel opening and closing
     * by itself while the reply streams shakes the conversation.
     */
    openWhileStreaming?: boolean
    /** Called right before the disclosure animates, on toggle and on streaming transitions. */
    onAnimationStart?: () => void
  }

function ReasoningRoot({
  className,
  variant,
  open: controlledOpen,
  onOpenChange: controlledOnOpenChange,
  defaultOpen = false,
  streaming,
  openWhileStreaming = false,
  onAnimationStart,
  children,
  ...props
}: ReasoningRootProps) {
  const [initialOpen] = useState(defaultOpen)
  const [userOpen, setUserOpen] = useState<boolean | null>(null)

  const isControlled = controlledOpen !== undefined
  const autoOpen = openWhileStreaming && streaming === true
  const isOpen = isControlled
    ? controlledOpen
    : (userOpen ?? (autoOpen || initialOpen))
  const isPreview = streaming === true && isOpen

  const prevStreamingRef = useRef(streaming)
  useLayoutEffect(() => {
    if (prevStreamingRef.current === streaming) return
    prevStreamingRef.current = streaming
    // A streaming transition only animates the panel when it opens by
    // itself and the resting state is collapsed; with `defaultOpen` the
    // disclosure stays open across it.
    if (
      openWhileStreaming &&
      !isControlled &&
      userOpen === null &&
      !initialOpen
    ) {
      onAnimationStart?.()
    }
  }, [
    streaming,
    openWhileStreaming,
    isControlled,
    userOpen,
    initialOpen,
    onAnimationStart,
  ])

  const handleOpenChange = useCallback(
    (open: boolean) => {
      onAnimationStart?.()
      if (!isControlled) {
        setUserOpen(open)
      }
      controlledOnOpenChange?.(open)
    },
    [onAnimationStart, isControlled, controlledOnOpenChange]
  )

  return (
    <Collapsible
      data-slot="reasoning-root"
      data-variant={variant}
      open={isOpen}
      onOpenChange={handleOpenChange}
      className={cn(
        "group/reasoning-root",
        reasoningVariants({ variant, className })
      )}
      style={
        {
          "--animation-duration": `${ANIMATION_DURATION}ms`,
        } as React.CSSProperties
      }
      {...props}
    >
      <ReasoningPreviewContext.Provider value={isPreview}>
        {children}
      </ReasoningPreviewContext.Provider>
    </Collapsible>
  )
}

function ReasoningFade({
  side = "bottom",
  className,
  ...props
}: React.ComponentProps<"div"> & { side?: "top" | "bottom" }) {
  if (side === "top") {
    return (
      <div
        data-slot="reasoning-fade"
        className={cn(
          "aui-reasoning-fade pointer-events-none absolute inset-x-0 top-0 z-10 h-8",
          "bg-[linear-gradient(to_bottom,var(--color-background),transparent)]",
          "group-data-[variant=muted]/reasoning-root:bg-[linear-gradient(to_bottom,color-mix(in_oklab,var(--color-muted)_50%,var(--color-background)),transparent)]",
          "animate-in fade-in-0",
          "animation-duration-(--animation-duration)",
          className
        )}
        {...props}
      />
    )
  }

  return (
    <div
      data-slot="reasoning-fade"
      className={cn(
        "aui-reasoning-fade pointer-events-none absolute inset-x-0 bottom-0 z-10 h-8",
        "bg-[linear-gradient(to_top,var(--color-background),transparent)]",
        "group-data-[variant=muted]/reasoning-root:bg-[linear-gradient(to_top,color-mix(in_oklab,var(--color-muted)_50%,var(--color-background)),transparent)]",
        "animate-in fade-in-0",
        "animation-duration-(--animation-duration)",
        className
      )}
      {...props}
    />
  )
}

function ReasoningTrigger({
  active,
  duration,
  className,
  ...props
}: React.ComponentProps<typeof CollapsibleTrigger> & {
  /** Still thinking: "Thinking", shimmering. */
  active?: boolean
  /** How long it thought, in seconds, once known: "Thought for 12 seconds". */
  duration?: number | undefined
}) {
  const label = active
    ? "Thinking"
    : duration
      ? `Thought for ${formatSeconds(duration)}`
      : "Thoughts"

  return (
    <CollapsibleTrigger
      data-slot="reasoning-trigger"
      className={cn(
        "aui-reasoning-trigger group/trigger flex max-w-[75%] origin-left items-center gap-2 py-1 text-sm text-muted-foreground transition-[color,scale] hover:text-foreground active:scale-[0.98]",
        className
      )}
      {...props}
    >
      <BrainIcon
        data-slot="reasoning-trigger-icon"
        className="aui-reasoning-trigger-icon size-4 shrink-0"
      />
      <span
        data-slot="reasoning-trigger-label"
        className={cn(
          "aui-reasoning-trigger-label-wrapper inline-block leading-none tabular-nums",
          active && "shimmer motion-reduce:animate-none"
        )}
      >
        {label}
      </span>
      {/* Down while closed, up while open. */}
      <ChevronDownIcon
        data-slot="reasoning-trigger-chevron"
        className={cn(
          "aui-reasoning-trigger-chevron size-4 shrink-0",
          "transition-transform duration-(--animation-duration) ease-[cubic-bezier(0.32,0.72,0,1)] motion-reduce:transition-none",
          "group-data-open/trigger:rotate-180",
          "group-data-panel-open/trigger:rotate-180"
        )}
      />
    </CollapsibleTrigger>
  )
}

function ReasoningContent({
  className,
  children,
  ...props
}: React.ComponentProps<typeof CollapsibleContent>) {
  const isPreview = useContext(ReasoningPreviewContext)

  return (
    <CollapsibleContent
      data-slot="reasoning-content"
      className={cn(
        "aui-reasoning-content relative overflow-hidden text-sm text-muted-foreground outline-none",
        "group/collapsible-content ease-[cubic-bezier(0.32,0.72,0,1)] motion-reduce:animate-none",
        "data-closed:animate-collapsible-up",
        "data-open:animate-collapsible-down",
        "data-closed:fill-mode-forwards",
        "data-closed:pointer-events-none",
        "[--tw-duration:var(--animation-duration)]",
        className
      )}
      {...props}
    >
      <ReasoningFade side="top" />
      {children}
      {isPreview ? <ReasoningFade /> : null}
    </CollapsibleContent>
  )
}

function ReasoningText({
  className,
  children,
  ...props
}: React.ComponentProps<"div">) {
  const isPreview = useContext(ReasoningPreviewContext)
  const scrollRef = useRef<HTMLDivElement>(null)
  const contentRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!isPreview) return
    const scrollEl = scrollRef.current
    const contentEl = contentRef.current
    if (!scrollEl || !contentEl) return

    let pinned = true
    let lastScrollTop = scrollEl.scrollTop
    let lastScrollHeight = scrollEl.scrollHeight
    const isAtBottom = () =>
      Math.abs(
        scrollEl.scrollHeight - scrollEl.scrollTop - scrollEl.clientHeight
      ) <= 1 || scrollEl.scrollHeight <= scrollEl.clientHeight

    const pin = () => {
      if (!pinned) return
      scrollEl.scrollTop = scrollEl.scrollHeight
    }
    // A pin's own scroll event can arrive after new content grew the scroll
    // height and read as "not at bottom"; only an upward move at unchanged
    // scroll height is user intent.
    const onScroll = () => {
      if (isAtBottom()) {
        pinned = true
      } else if (
        scrollEl.scrollTop < lastScrollTop &&
        scrollEl.scrollHeight === lastScrollHeight
      ) {
        pinned = false
      }
      lastScrollTop = scrollEl.scrollTop
      lastScrollHeight = scrollEl.scrollHeight
    }

    pin()
    scrollEl.addEventListener("scroll", onScroll)
    const observer = new ResizeObserver(pin)
    observer.observe(contentEl)
    return () => {
      scrollEl.removeEventListener("scroll", onScroll)
      observer.disconnect()
    }
  }, [isPreview])

  return (
    <div
      ref={scrollRef}
      data-slot="reasoning-text"
      className={cn(
        "aui-reasoning-text relative z-0 max-h-64 overflow-y-auto pt-3 pb-1 leading-relaxed text-pretty",
        "transform-gpu transition-[transform,opacity] ease-[cubic-bezier(0.32,0.72,0,1)]",
        "motion-reduce:animate-none",
        "group-data-open/collapsible-content:animate-in",
        "group-data-closed/collapsible-content:animate-out",
        "group-data-open/collapsible-content:fade-in-0",
        "group-data-closed/collapsible-content:fade-out-0",
        "group-data-open/collapsible-content:slide-in-from-top-4",
        "group-data-closed/collapsible-content:slide-out-to-top-4",
        "group-data-open/collapsible-content:blur-in-[2px]",
        "group-data-closed/collapsible-content:blur-out-[2px]",
        "group-data-open/collapsible-content:animation-duration-(--animation-duration)",
        "group-data-closed/collapsible-content:animation-duration-(--animation-duration)",
        className
      )}
      {...props}
    >
      <div ref={contentRef} className="aui-reasoning-text-content space-y-4">
        {children}
      </div>
    </div>
  )
}

export {
  ReasoningRoot,
  ReasoningTrigger,
  ReasoningContent,
  ReasoningText,
  ReasoningFade,
  reasoningVariants,
}
