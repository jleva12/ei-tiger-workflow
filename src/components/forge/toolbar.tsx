import * as React from "react"
import { cn } from "cn"

import { Button } from "@/components/ui/button"
import {
  InputGroup,
  InputGroupAddon,
  InputGroupInput,
} from "@/components/ui/input-group"
import { Kbd } from "@/components/ui/kbd"
import { TabsList, TabsTrigger } from "@/components/ui/tabs"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import { Icon } from "./icon"
import type { IconProp } from "./icons"

/** Borderless tab strip for switching views (List, Kanban, Activity…). */
function ViewTabsList({
  className,
  ...props
}: React.ComponentProps<typeof TabsList>) {
  return (
    <TabsList
      className={cn(
        "h-8! gap-[5px] bg-transparent p-0 @max-[600px]/shell:gap-0.5",
        className
      )}
      {...props}
    />
  )
}

/** A view tab: the active tab gets a hairline ring and a faint fill. */
function ViewTabsTrigger({
  icon,
  className,
  children,
  ...props
}: React.ComponentProps<typeof TabsTrigger> & { icon?: IconProp }) {
  return (
    <TabsTrigger
      className={cn(
        "h-8 flex-none gap-[7px] rounded-(--radius-control) px-[11px] py-0 text-[0.8125rem] font-normal text-muted-foreground @max-[1270px]/shell:px-2 @max-[1270px]/shell:text-xs @max-[800px]/shell:px-3 @max-[600px]/shell:gap-[5px] @max-[600px]/shell:px-[9px] dark:text-muted-foreground data-active:bg-[color-mix(in_oklch,var(--muted)_50%,var(--background))] data-active:text-foreground data-active:shadow-(--shadow-tab) dark:data-active:border-transparent dark:data-active:bg-[color-mix(in_oklch,var(--muted)_50%,var(--background))]",
        className
      )}
      {...props}
    >
      {icon && <Icon icon={icon} />}
      {children}
    </TabsTrigger>
  )
}

type LayoutOption = { value: string; label: string; icon: IconProp }

/** Compact segmented icon switch (e.g. Kanban / List). */
function LayoutSwitch({
  value,
  onValueChange,
  options,
  className,
}: {
  value: string
  onValueChange: (value: string) => void
  options: LayoutOption[]
  className?: string
}) {
  return (
    <ToggleGroup
      aria-label="Layout"
      value={[value]}
      onValueChange={(next) => {
        if (next[0]) onValueChange(String(next[0]))
      }}
      className={cn(
        "gap-0 rounded-(--radius-control) bg-muted p-[3px] @max-[800px]/shell:hidden",
        className
      )}
    >
      {options.map((option) => (
        <ToggleGroupItem
          key={option.value}
          value={option.value}
          aria-label={option.label}
          className="size-6 min-w-6 px-0 hover:bg-muted aria-pressed:bg-background aria-pressed:shadow-(--shadow-raised)"
        >
          <Icon icon={option.icon} size={16} />
        </ToggleGroupItem>
      ))}
    </ToggleGroup>
  )
}

/** Header search with a leading glyph and a trailing shortcut hint. */
function SearchField({
  shortcut = "/",
  className,
  "aria-label": ariaLabel = "Search",
  placeholder = "Search",
  ...props
}: React.ComponentProps<"input"> & { shortcut?: string }) {
  return (
    <InputGroup
      className={cn(
        "h-8 w-[174px] text-muted-foreground @max-[1270px]/shell:w-[150px] @max-[800px]/shell:w-[145px] @max-[600px]/shell:w-[118px]",
        className
      )}
    >
      <InputGroupAddon>
        <Icon icon="search" />
      </InputGroupAddon>
      <InputGroupInput
        aria-label={ariaLabel}
        placeholder={placeholder}
        className="text-xs md:text-xs"
        {...props}
      />
      {shortcut && (
        <InputGroupAddon align="inline-end">
          <Kbd variant="ghost" className="text-2xs">
            {shortcut}
          </Kbd>
        </InputGroupAddon>
      )}
    </InputGroup>
  )
}

/**
 * Outline toolbar control. `value` renders an emphasised current setting
 * ("Group by **Status**"); `active` marks an applied filter with a dot.
 * Use as `render` of a DropdownMenuTrigger to open a menu.
 */
function ToolbarButton({
  icon,
  value,
  active = false,
  className,
  children,
  ...props
}: React.ComponentProps<typeof Button> & {
  icon?: IconProp
  value?: React.ReactNode
  active?: boolean
}) {
  return (
    <Button
      variant="outline"
      data-active={active || undefined}
      className={cn(
        "text-[0.8125rem] font-normal whitespace-nowrap data-active:border-subtle/60",
        value !== undefined &&
          "bg-[color-mix(in_oklch,var(--muted)_35%,var(--background))] @max-[600px]/shell:text-2xs dark:bg-[color-mix(in_oklch,var(--muted)_35%,var(--background))]",
        className
      )}
      {...props}
    >
      {icon && <Icon icon={icon} size={14} />}
      {children}
      {value !== undefined && <strong className="font-medium">{value}</strong>}
      {active && (
        <span
          aria-hidden="true"
          className="size-[5px] rounded-full bg-foreground"
        />
      )}
    </Button>
  )
}

export {
  ViewTabsList,
  ViewTabsTrigger,
  LayoutSwitch,
  SearchField,
  ToolbarButton,
}
