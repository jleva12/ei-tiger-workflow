import * as React from "react"
import { cn } from "cn"

import { Button } from "@/components/ui/button"
import { DropdownMenuTrigger } from "@/components/ui/dropdown-menu"
import { Kbd } from "@/components/ui/kbd"
import { PopoverTrigger } from "@/components/ui/popover"
import { Separator } from "@/components/ui/separator"
import { Spinner } from "@/components/ui/spinner"
import { Toggle } from "@/components/ui/toggle"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { Icon } from "../icon"
import { iconNames, type IconProp } from "../icons"
import { formatShortcut } from "./shortcuts"
import type { EditorAction } from "./types"

const NAMES = new Set<string>(iconNames)

/** Whether `icon` is a Hugeicons glyph (an array of SVG element tuples). */
function isGlyph(icon: unknown): icon is IconProp {
  return (
    Array.isArray(icon) &&
    Array.isArray(icon[0]) &&
    typeof icon[0][0] === "string"
  )
}

/**
 * An action's icon, which may be a React node, a Forge icon name or a
 * Hugeicons glyph. Strings that aren't icon names render as text.
 */
export function ActionIcon({
  icon,
  size,
}: {
  icon: EditorAction["icon"]
  size?: number
}) {
  if (icon === undefined || icon === null || icon === false) return null
  if (typeof icon === "string" && NAMES.has(icon)) {
    return <Icon icon={icon as IconProp} size={size} />
  }
  if (isGlyph(icon)) return <Icon icon={icon} size={size} />
  return <>{icon as React.ReactNode}</>
}

function Hint({ label, shortcut }: { label: string; shortcut?: string }) {
  return (
    <TooltipContent side="bottom">
      {label}
      {shortcut && <Kbd>{formatShortcut(shortcut)}</Kbd>}
    </TooltipContent>
  )
}

/** An on/off formatting button (bold, bulleted list, …) with its shortcut. */
export function ToolbarToggle({
  icon,
  label,
  shortcut,
  pressed,
  disabled,
  onPressedChange,
}: {
  icon: EditorAction["icon"]
  label: string
  shortcut?: string
  pressed: boolean
  disabled?: boolean
  onPressedChange: () => void
}) {
  return (
    <Tooltip>
      <TooltipTrigger
        render={
          <Toggle
            size="sm"
            aria-label={label}
            pressed={pressed}
            disabled={disabled}
            onPressedChange={onPressedChange}
            className="min-w-7 px-1.5 text-muted-foreground aria-pressed:text-foreground"
          />
        }
      >
        <ActionIcon icon={icon} />
      </TooltipTrigger>
      <Hint label={label} shortcut={shortcut} />
    </Tooltip>
  )
}

/** A one-shot action (undo, clear formatting, …). */
export function ToolbarAction({
  icon,
  label,
  shortcut,
  disabled,
  busy,
  onClick,
  className,
}: {
  icon: EditorAction["icon"]
  label: string
  shortcut?: string
  disabled?: boolean
  /** Shows a spinner while the action runs. */
  busy?: boolean
  onClick: () => void
  className?: string
}) {
  return (
    <Tooltip>
      <TooltipTrigger
        render={
          <Button
            type="button"
            variant="ghost"
            size="icon-sm"
            aria-label={label}
            aria-busy={busy || undefined}
            disabled={disabled}
            onClick={onClick}
            className={cn("text-muted-foreground", className)}
          />
        }
      >
        {busy ? <Spinner /> : <ActionIcon icon={icon} />}
      </TooltipTrigger>
      <Hint label={label} shortcut={shortcut} />
    </Tooltip>
  )
}

/**
 * The trigger of a toolbar dropdown: an icon (or a short text value) and a
 * caret, with a tooltip. Render it inside `<DropdownMenu>`, or inside
 * `<Popover>` with `popover` for panels that hold plain buttons.
 */
export function ToolbarMenuTrigger({
  icon,
  label,
  value,
  disabled,
  active,
  popover = false,
  className,
  children,
}: {
  icon?: EditorAction["icon"]
  label: string
  value?: React.ReactNode
  disabled?: boolean
  active?: boolean
  popover?: boolean
  className?: string
  children?: React.ReactNode
}) {
  const Trigger = popover ? PopoverTrigger : DropdownMenuTrigger
  return (
    <Tooltip>
      <TooltipTrigger
        render={
          <Trigger
            render={
              <Button
                type="button"
                variant="ghost"
                size="sm"
                aria-label={label}
                disabled={disabled}
                data-active={active || undefined}
                className={cn(
                  "gap-0.5 px-1.5 font-normal text-muted-foreground data-popup-open:bg-muted data-popup-open:text-foreground data-active:bg-muted data-active:text-foreground",
                  className
                )}
              />
            }
          />
        }
      >
        <ActionIcon icon={icon} />
        {value !== undefined && (
          <span className="truncate text-xs text-foreground">{value}</span>
        )}
        {children}
        <Icon icon="down" className="size-3 opacity-60" />
      </TooltipTrigger>
      <Hint label={label} />
    </Tooltip>
  )
}

export function ToolbarSeparator() {
  return (
    <Separator
      orientation="vertical"
      className="mx-0.5 h-4 self-center data-vertical:self-center"
    />
  )
}
