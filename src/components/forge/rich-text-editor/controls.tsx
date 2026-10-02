import * as React from "react"
import { cn } from "cn"

import { Button } from "@/components/ui/button"
import { DropdownMenuTrigger } from "@/components/ui/dropdown-menu"
import { Kbd } from "@/components/ui/kbd"
import { PopoverTrigger } from "@/components/ui/popover"
import { Separator } from "@/components/ui/separator"
import { Toggle } from "@/components/ui/toggle"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { Icon } from "../icon"
import type { IconProp } from "../icons"
import type { Swatch } from "./palette"
import { formatShortcut } from "./shortcuts"

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
  icon: IconProp
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
        <Icon icon={icon} />
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
  onClick,
  className,
}: {
  icon: IconProp
  label: string
  shortcut?: string
  disabled?: boolean
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
            disabled={disabled}
            onClick={onClick}
            className={cn("text-muted-foreground", className)}
          />
        }
      >
        <Icon icon={icon} />
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
  icon?: IconProp
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
        {icon && <Icon icon={icon} />}
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

/**
 * A row of colour swatches. Text colours show an "A" in the colour;
 * highlights show the fill. The chosen one is marked.
 */
export function SwatchGrid({
  label,
  swatches,
  kind,
  current,
  onSelect,
}: {
  label: string
  swatches: Swatch[]
  kind: "text" | "highlight"
  current: string | null
  onSelect: (value: string | null) => void
}) {
  return (
    <div
      role="group"
      aria-label={label}
      className="grid grid-cols-9 gap-1 px-1.5 pb-1.5"
    >
      {swatches.map((swatch) => {
        const selected = (current ?? null) === swatch.value
        return (
          <Tooltip key={swatch.name}>
            <TooltipTrigger
              render={
                <button
                  type="button"
                  aria-label={`${label}: ${swatch.name}`}
                  aria-pressed={selected}
                  onClick={() => onSelect(swatch.value)}
                  className="grid size-6 place-items-center overflow-hidden rounded-(--radius-item) border border-border text-xs font-semibold transition-shadow hover:shadow-(--shadow-raised) aria-pressed:border-foreground"
                  style={
                    swatch.value === null
                      ? undefined
                      : kind === "text"
                        ? { color: swatch.value }
                        : { background: swatch.value }
                  }
                />
              }
            >
              {swatch.value === null ? (
                <span
                  aria-hidden="true"
                  className="h-px w-8 -rotate-45 bg-muted-foreground/60"
                />
              ) : kind === "text" ? (
                "A"
              ) : null}
            </TooltipTrigger>
            <TooltipContent side="bottom">{swatch.name}</TooltipContent>
          </Tooltip>
        )
      })}
    </div>
  )
}
