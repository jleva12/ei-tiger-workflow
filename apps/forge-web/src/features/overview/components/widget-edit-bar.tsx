import * as React from "react"
import { PencilEdit02Icon } from "@hugeicons/core-free-icons"
import { cn } from "cn"

import { TooltipIconButton } from "@/components/assistant-ui/elements/tooltip-icon-button"
import { Icon } from "@/components/forge/icon"
import { Field, FieldDescription, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover"
import { TITLE_LENGTH } from "@/features/overview/lib/dashboard"

/*
 * A widget's bar while its dashboard is customized: the handle it's dragged
 * by, its type, its own settings (such as its chart's type), and buttons to
 * rename or remove it. Under 900px of shell the
 * widgets stack, so the handle goes and only renaming and removing stay.
 */

/** The class react-grid-layout drags widgets by. */
export const DRAG_HANDLE = "forge-dashboard-handle"

export function WidgetEditBar({
  name,
  title,
  controls,
  onRename,
  onRemove,
}: {
  /** The widget's type, as its panel titles it. */
  name: string
  /** The widget's own title, if it has one. */
  title: string | undefined
  /** Its type's settings, before rename and remove. */
  controls?: React.ReactNode
  /** Without it, the widget has no title to rename. */
  onRename?: (title: string) => void
  onRemove: () => void
}) {
  return (
    <div className="-mx-1 mb-3 flex h-7 items-center gap-1 rounded-(--radius-control) bg-muted px-1 text-2xs text-muted-foreground">
      <div
        className={cn(
          DRAG_HANDLE,
          // Stacked under 900px, so not dragged there.
          "flex h-full min-w-0 flex-1 cursor-grab items-center gap-1.5 active:cursor-grabbing @max-[900px]/shell:pointer-events-none"
        )}
        title="Drag to move"
      >
        <Icon icon="grip" size={15} className="@max-[900px]/shell:hidden" />
        <span className="truncate">{name}</span>
      </div>
      {controls}
      {onRename && (
        <RenameWidget name={name} title={title} onRename={onRename} />
      )}
      <TooltipIconButton
        tooltip={`Remove ${title ?? name}`}
        side="top"
        onClick={onRemove}
        className="size-6 text-muted-foreground hover:text-foreground"
      >
        <Icon icon="close" size={14} />
      </TooltipIconButton>
    </div>
  )
}

function RenameWidget({
  name,
  title,
  onRename,
}: {
  name: string
  title: string | undefined
  onRename: (title: string) => void
}) {
  const [open, setOpen] = React.useState(false)
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger
        render={
          <TooltipIconButton
            tooltip={`Rename ${title ?? name}`}
            side="top"
            className="size-6 text-muted-foreground hover:text-foreground"
          />
        }
      >
        <Icon icon={PencilEdit02Icon} size={14} />
      </PopoverTrigger>
      <PopoverContent align="end" side="bottom" className="w-64">
        {open && (
          <TitleField
            name={name}
            title={title}
            onRename={onRename}
            onDone={() => setOpen(false)}
          />
        )}
      </PopoverContent>
    </Popover>
  )
}

function TitleField({
  name,
  title,
  onRename,
  onDone,
}: {
  name: string
  title: string | undefined
  onRename: (title: string) => void
  onDone: () => void
}) {
  const id = React.useId()
  // As typed, spaces and all; the layout keeps it trimmed.
  const [value, setValue] = React.useState(title ?? "")
  return (
    <Field>
      <FieldLabel htmlFor={id}>Title</FieldLabel>
      <Input
        id={id}
        autoFocus
        value={value}
        placeholder={name}
        maxLength={TITLE_LENGTH}
        onChange={(event) => {
          setValue(event.target.value)
          onRename(event.target.value)
        }}
        onKeyDown={(event) => {
          if (event.key === "Enter") onDone()
        }}
      />
      <FieldDescription>Leave it empty to call it {name}.</FieldDescription>
    </Field>
  )
}
