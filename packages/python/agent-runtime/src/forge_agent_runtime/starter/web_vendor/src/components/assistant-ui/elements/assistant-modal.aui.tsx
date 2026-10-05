"use client"

import {
  type ComponentPropsWithoutRef,
  type FC,
  type KeyboardEvent,
  type PointerEvent,
  type ReactNode,
  type RefObject,
  forwardRef,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react"
import {
  ThreadListPrimitive,
  useAuiEvent,
  useAuiState,
} from "@assistant-ui/react"
import { Popover as PopoverPrimitive } from "@base-ui/react/popover"
import { cn } from "cn"

import {
  BotIcon,
  ChevronDownIcon,
  HistoryIcon,
  PlusIcon,
} from "@/components/assistant-ui/elements/aui-icons"
import { Thread } from "@/components/assistant-ui/elements/thread.aui"
import {
  ThreadListItems,
  ThreadListRoot,
  ThreadListSearch,
} from "@/components/assistant-ui/elements/thread-list.aui"
import { TooltipIconButton } from "@/components/assistant-ui/elements/tooltip-icon-button"

type ModalView = "thread" | "list"

type ModalSize = { readonly width: number; readonly height: number }

type ResizeDrag = {
  readonly pointerId: number
  readonly x: number
  readonly y: number
  readonly width: number
  readonly height: number
  readonly direction: 1 | -1
  last: ModalSize | null
}

const SIZE_STORAGE_KEY = "aui-modal-size"
const MIN_SIZE: ModalSize = { width: 320, height: 400 }
// The trigger and its offsets sit below the modal, so the height leaves room for them.
const VIEWPORT_INSET: ModalSize = { width: 32, height: 96 }
const RESIZE_STEP = 16

const clampSize = ({ width, height }: ModalSize): ModalSize => ({
  width: Math.round(
    Math.max(
      MIN_SIZE.width,
      Math.min(width, window.innerWidth - VIEWPORT_INSET.width)
    )
  ),
  height: Math.round(
    Math.max(
      MIN_SIZE.height,
      Math.min(height, window.innerHeight - VIEWPORT_INSET.height)
    )
  ),
})

const readStoredSize = (): ModalSize | null => {
  if (typeof window === "undefined") return null
  try {
    const stored: unknown = JSON.parse(
      window.localStorage.getItem(SIZE_STORAGE_KEY) ?? "null"
    )
    if (typeof stored !== "object" || stored === null) return null
    const { width, height } = stored as Record<string, unknown>
    if (typeof width !== "number" || typeof height !== "number") return null
    return clampSize({ width, height })
  } catch {
    return null
  }
}

const storeSize = (size: ModalSize | null) => {
  try {
    if (size) {
      window.localStorage.setItem(SIZE_STORAGE_KEY, JSON.stringify(size))
    } else {
      window.localStorage.removeItem(SIZE_STORAGE_KEY)
    }
  } catch {
    // Without storage the size lasts until the page reloads.
  }
}

const isRtl = (element: Element) =>
  getComputedStyle(element).direction === "rtl"

const useModalSize = (contentRef: RefObject<HTMLDivElement | null>) => {
  const [size, setSize] = useState(readStoredSize)
  const dragRef = useRef<ResizeDrag | null>(null)

  const commitSize = (next: ModalSize | null) => {
    setSize(next)
    storeSize(next)
  }

  const sizeFromPointer = (
    drag: ResizeDrag,
    event: PointerEvent<HTMLElement>
  ) =>
    clampSize({
      width: drag.width - (event.clientX - drag.x) * drag.direction,
      height: drag.height - (event.clientY - drag.y),
    })

  const takeDrag = (event: PointerEvent<HTMLElement>) => {
    const drag = dragRef.current
    if (!drag || drag.pointerId !== event.pointerId) return null
    dragRef.current = null
    return drag
  }

  const reset = () => {
    dragRef.current = null
    commitSize(null)
  }

  return {
    size,
    reset,
    handleProps: {
      onPointerDown: (event: PointerEvent<HTMLElement>) => {
        const content = contentRef.current
        if (event.button !== 0 || !content) return
        const rect = content.getBoundingClientRect()
        dragRef.current = {
          pointerId: event.pointerId,
          x: event.clientX,
          y: event.clientY,
          width: rect.width,
          height: rect.height,
          direction: isRtl(content) ? -1 : 1,
          last: null,
        }
        event.currentTarget.setPointerCapture(event.pointerId)
        event.preventDefault()
      },
      onPointerMove: (event: PointerEvent<HTMLElement>) => {
        const drag = dragRef.current
        if (!drag || drag.pointerId !== event.pointerId) return
        if (!drag.last && event.clientX === drag.x && event.clientY === drag.y)
          return
        drag.last = sizeFromPointer(drag, event)
        setSize(drag.last)
      },
      onPointerUp: (event: PointerEvent<HTMLElement>) => {
        const drag = takeDrag(event)
        if (!drag) return
        if (drag.last || event.clientX !== drag.x || event.clientY !== drag.y) {
          commitSize(sizeFromPointer(drag, event))
        }
      },
      onPointerCancel: (event: PointerEvent<HTMLElement>) => {
        const drag = takeDrag(event)
        if (drag?.last) commitSize(drag.last)
      },
      onKeyDown: (event: KeyboardEvent<HTMLElement>) => {
        const content = contentRef.current
        if (!content) return
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault()
          reset()
          return
        }
        const step = event.shiftKey ? RESIZE_STEP * 4 : RESIZE_STEP
        const rtl = isRtl(content)
        const changes: Record<string, readonly [number, number]> = {
          ArrowUp: [0, step],
          ArrowDown: [0, -step],
          [rtl ? "ArrowRight" : "ArrowLeft"]: [step, 0],
          [rtl ? "ArrowLeft" : "ArrowRight"]: [-step, 0],
        }
        const change = changes[event.key]
        if (!change) return
        event.preventDefault()
        const rect = content.getBoundingClientRect()
        commitSize(
          clampSize({
            width: rect.width + change[0],
            height: rect.height + change[1],
          })
        )
      },
    },
  }
}

export type AssistantModalProps = {
  /** The conversation. Default: `<Thread />`. */
  thread?: ReactNode
  /** Extra header controls, before the conversations toggle. */
  headerActions?: ReactNode
  /** Shown between the header and the conversation, e.g. a status banner. */
  banner?: ReactNode
  /** Names the trigger ("Open Assistant"). Default "Assistant". */
  label?: string
  /** Start with the panel open. */
  defaultOpen?: boolean
  /** Whether the panel is open, to control it; `defaultOpen` otherwise. */
  open?: boolean
  /** Called when the panel opens or closes. */
  onOpenChange?: (open: boolean) => void
  /**
   * The launcher's icon while closed; it crossfades to a chevron once open.
   * Hover effects can target the button as `group/launcher`. Default: a bot.
   */
  icon?: ReactNode
  /**
   * Classes for the trigger's anchor, which is fixed to the viewport's
   * bottom-end corner by default; `absolute` pins it inside a positioned
   * container instead.
   */
  className?: string
}

export const AssistantModal: FC<AssistantModalProps> = ({
  thread,
  headerActions,
  banner,
  label = "Assistant",
  defaultOpen = false,
  open: openProp,
  onOpenChange,
  icon,
  className,
}) => {
  const [openState, setOpenState] = useState(defaultOpen)
  const open = openProp ?? openState
  const setOpen = (next: boolean) => {
    setOpenState(next)
    onOpenChange?.(next)
  }
  const [view, setView] = useState<ModalView>("thread")
  const contentRef = useRef<HTMLDivElement>(null)
  const { size, reset, handleProps } = useModalSize(contentRef)
  const conversation = useMemo(() => thread ?? <Thread />, [thread])

  useAuiEvent("thread.runStart", () => {
    setView("thread")
    setOpen(true)
  })

  return (
    <PopoverPrimitive.Root
      open={open}
      onOpenChange={(open, eventDetails) => {
        if (
          !open &&
          (eventDetails.reason === "outside-press" ||
            eventDetails.reason === "focus-out")
        ) {
          eventDetails.cancel()
        } else {
          if (open) setView("thread")
          setOpen(open)
        }
      }}
    >
      <div
        className={cn(
          "aui-root aui-modal-anchor fixed end-4 bottom-4 z-40 size-11",
          className
        )}
      >
        <PopoverPrimitive.Trigger
          render={(props, state) => (
            <AssistantModalButton
              {...props}
              open={state.open}
              label={label}
              icon={icon}
            />
          )}
        />
      </div>
      <PopoverPrimitive.Portal>
        <PopoverPrimitive.Positioner
          side="top"
          align="end"
          sideOffset={16}
          positionMethod="fixed"
          className="isolate z-50"
        >
          <PopoverPrimitive.Popup
            ref={contentRef}
            style={size ?? undefined}
            className="group/modal aui-root aui-modal-content z-50 flex h-125 max-h-(--available-height) w-100 max-w-[calc(100vw-2rem)] origin-(--transform-origin) flex-col gap-0 overflow-clip overscroll-contain rounded-xl bg-popover p-0 text-base text-popover-foreground antialiased shadow-[0_16px_48px_-24px_rgb(0_0_0/0.25)] ring-1 ring-foreground/10 transition-none ease-[cubic-bezier(0.32,0.72,0,1)] outline-none motion-reduce:animate-none dark:shadow-[0_16px_48px_-24px_rgb(0_0_0/0.6)] data-open:animate-in data-open:duration-300 data-open:fade-in-0 data-open:zoom-in-95 data-open:slide-in-from-bottom-2 data-closed:animate-out data-closed:duration-200 data-closed:fade-out-0 data-closed:zoom-out-95 data-closed:slide-out-to-bottom-2 [&_.aui-thread-root]:bg-inherit [&_.aui-thread-viewport-footer]:bg-popover motion-reduce:[&_.aui-thread-viewport-footer]:animate-none [&_[data-slot=aui\_thread-viewport]]:scrollbar-panel [&_[data-slot=aui\_thread-viewport]]:[scrollbar-gutter:stable_both-edges] [&[data-open]_.aui-thread-viewport-footer]:animate-in [&[data-open]_.aui-thread-viewport-footer]:delay-100 [&[data-open]_.aui-thread-viewport-footer]:duration-300 [&[data-open]_.aui-thread-viewport-footer]:ease-[cubic-bezier(0.32,0.72,0,1)] [&[data-open]_.aui-thread-viewport-footer]:fade-in-0 [&[data-open]_.aui-thread-viewport-footer]:fill-mode-backwards [&[data-open]_.aui-thread-viewport-footer]:slide-in-from-bottom-2"
          >
            <AssistantModalResizeHandle
              {...handleProps}
              onDoubleClick={reset}
            />
            <AssistantModalHeader
              view={view}
              onViewChange={setView}
              actions={headerActions}
            />
            {banner}
            <div className="aui-modal-body relative min-h-0 flex-1">
              <div
                ref={(node) => {
                  if (node) node.inert = view === "list"
                }}
                className="aui-modal-thread h-full"
              >
                {conversation}
              </div>
              {view === "list" && (
                <AssistantModalThreadList onSelect={() => setView("thread")} />
              )}
            </div>
          </PopoverPrimitive.Popup>
        </PopoverPrimitive.Positioner>
      </PopoverPrimitive.Portal>
    </PopoverPrimitive.Root>
  )
}

const AssistantModalHeader: FC<{
  view: ModalView
  onViewChange: (view: ModalView) => void
  actions: ReactNode
}> = ({ view, onViewChange, actions }) => {
  const title = useAuiState((s) => s.threadListItem.title)
  const hasThreads = useAuiState((s) => s.threads.threadIds.length > 0)
  const titleRef = useRef<HTMLHeadingElement>(null)
  const shownViewRef = useRef(view)

  useEffect(() => {
    if (shownViewRef.current === view) return
    shownViewRef.current = view
    const heading = titleRef.current
    const active = document.activeElement
    if (
      active === document.body ||
      active === heading?.closest("[role='dialog']")
    ) {
      heading?.focus()
    }
  }, [view])

  return (
    <div className="aui-modal-header flex h-11 shrink-0 items-center gap-2 border-b border-foreground/10 ps-3.5 pe-2">
      <PopoverPrimitive.Title
        ref={titleRef}
        tabIndex={-1}
        className="aui-modal-title min-w-0 flex-1 truncate text-[0.8125rem] font-medium outline-none"
      >
        {view === "list" ? "Conversations" : title || "New conversation"}
      </PopoverPrimitive.Title>
      <div className="flex shrink-0 items-center gap-0.5">
        {actions}
        <TooltipIconButton
          tooltip="Conversations"
          side="bottom"
          aria-pressed={view === "list"}
          disabled={!hasThreads && view === "thread"}
          className="aui-modal-threads size-7 rounded-md p-0 text-muted-foreground hover:text-foreground aria-pressed:bg-muted aria-pressed:text-foreground"
          onClick={() => onViewChange(view === "list" ? "thread" : "list")}
        >
          <HistoryIcon className="size-3.5" />
        </TooltipIconButton>
        <ThreadListPrimitive.New
          render={
            <TooltipIconButton
              tooltip="New conversation"
              side="bottom"
              className="aui-modal-new size-7 rounded-md p-0 text-muted-foreground hover:text-foreground"
              onClick={() => onViewChange("thread")}
            />
          }
        >
          <PlusIcon className="size-3.5" />
        </ThreadListPrimitive.New>
      </div>
    </div>
  )
}

const AssistantModalThreadList: FC<{ onSelect: () => void }> = ({
  onSelect,
}) => {
  const [search, setSearch] = useState("")
  const hasThreads = useAuiState((s) => s.threads.threadIds.length > 0)

  return (
    <ThreadListRoot
      className="aui-modal-thread-list absolute inset-0 overflow-y-auto bg-popover p-2"
      onClick={(event) => {
        const target = event.target as Element
        if (target.closest("[data-slot='aui_thread-list-item-trigger']")) {
          onSelect()
        }
      }}
    >
      {hasThreads && (
        <ThreadListSearch value={search} onValueChange={setSearch} />
      )}
      <ThreadListItems searchQuery={hasThreads ? search : ""} />
    </ThreadListRoot>
  )
}

const AssistantModalResizeHandle: FC<ComponentPropsWithoutRef<"button">> = (
  props
) => {
  return (
    <button
      type="button"
      aria-label="Resize Assistant"
      className="aui-modal-resize-handle absolute start-0 top-0 z-10 size-6 cursor-nwse-resize touch-none rounded-ss-xl border-s-2 border-t-2 border-transparent transition-colors outline-none [clip-path:polygon(0_0,100%_0,100%_6px,6px_6px,6px_100%,0_100%)] group-hover/modal:border-foreground/15 hover:border-foreground/40 focus-visible:border-ring motion-reduce:transition-none rtl:cursor-nesw-resize rtl:[clip-path:polygon(0_0,100%_0,100%_100%,calc(100%_-_6px)_100%,calc(100%_-_6px)_6px,0_6px)]"
      {...props}
    />
  )
}

export type AssistantModalButtonProps = Omit<
  ComponentPropsWithoutRef<typeof TooltipIconButton>,
  "tooltip"
> & {
  open: boolean
  label: string
  icon: ReactNode
}

/** The round launcher, with `icon` while closed and a chevron while open. */
export const AssistantModalButton = forwardRef<
  HTMLButtonElement,
  AssistantModalButtonProps
>(({ open, label, icon, ...rest }, ref) => {
  const tooltip = open ? `Close ${label}` : `Open ${label}`

  return (
    <TooltipIconButton
      variant="ghost"
      tooltip={tooltip}
      side="left"
      {...rest}
      className="group/launcher aui-modal-button size-full rounded-full border border-border/60 bg-background text-foreground shadow-(--shadow-float) transition-[border-color,scale] duration-150 ease-out hover:border-border hover:bg-background active:scale-96 motion-reduce:transition-none"
      ref={ref}
    >
      <span
        data-open={open ? "" : undefined}
        data-closed={open ? undefined : ""}
        className="aui-modal-button-closed-icon absolute grid place-items-center transition-[scale,opacity,filter] duration-200 ease-[cubic-bezier(0.2,0,0,1)] motion-reduce:transition-none data-open:scale-25 data-open:opacity-0 data-open:blur-[4px] data-closed:scale-100 data-closed:opacity-100 data-closed:blur-[0px]"
      >
        {icon ?? <BotIcon className="size-5" />}
      </span>

      <ChevronDownIcon
        data-open={open ? "" : undefined}
        data-closed={open ? undefined : ""}
        className="aui-modal-button-open-icon absolute size-5 transition-[scale,opacity,filter] duration-200 ease-[cubic-bezier(0.2,0,0,1)] motion-reduce:transition-none data-open:scale-100 data-open:opacity-100 data-open:blur-[0px] data-closed:scale-25 data-closed:opacity-0 data-closed:blur-[4px]"
      />
      <span className="aui-sr-only sr-only">{tooltip}</span>
    </TooltipIconButton>
  )
})

AssistantModalButton.displayName = "AssistantModalButton"
