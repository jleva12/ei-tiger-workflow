import type { ReactNode } from "react"

import { Icon } from "@/components/forge/icon"
import type { IconProp } from "@/components/forge/icons"
import { Illustration } from "@/components/forge/illustration"
import { Button } from "@/components/ui/button"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"

type RowMenuProps = {
  /** Names the row for screen readers: "Actions for <label>". */
  label: string
  onEdit?: () => void
  onDelete?: () => void
  editLabel?: string
  editIcon?: IconProp
  deleteLabel?: string
  deleteIcon?: IconProp
  /** More items, between the edit and the destructive action. */
  children?: ReactNode
}

/** A table row's "…" menu: edit, any more items, then a destructive action. */
export function RowMenu({
  label,
  onEdit,
  onDelete,
  editLabel = "Edit…",
  editIcon = "settings",
  deleteLabel = "Delete…",
  deleteIcon = "close",
  children,
}: RowMenuProps) {
  if (!onEdit && !onDelete && !children) return null
  return (
    <DropdownMenu>
      <DropdownMenuTrigger
        render={
          <Button
            variant="ghost"
            size="icon-xs"
            aria-label={`Actions for ${label}`}
            // Keep a click on the menu from also clicking the row.
            onClick={(event) => event.stopPropagation()}
          />
        }
      >
        <Icon icon="more" />
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="min-w-40">
        {onEdit && (
          <DropdownMenuItem onClick={onEdit}>
            <Icon icon={editIcon} />
            {editLabel}
          </DropdownMenuItem>
        )}
        {children && (
          <>
            {onEdit && <DropdownMenuSeparator />}
            {children}
          </>
        )}
        {onDelete && (
          <>
            {(onEdit || children) && <DropdownMenuSeparator />}
            <DropdownMenuItem variant="destructive" onClick={onDelete}>
              <Icon icon={deleteIcon} />
              {deleteLabel}
            </DropdownMenuItem>
          </>
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

/** A table's empty state, offering to create the first row. */
export function EmptyRows({
  message,
  createLabel,
  onCreate,
}: {
  message: string
  createLabel?: string
  onCreate?: () => void
}) {
  return (
    <div className="flex flex-col items-center gap-3 py-4 text-xs text-subtle">
      <Illustration name="tasks" size="sm" />
      <p>{message}</p>
      {onCreate && createLabel && (
        <Button variant="outline" size="sm" onClick={onCreate}>
          <Icon icon="plus" data-icon="inline-start" />
          {createLabel}
        </Button>
      )}
    </div>
  )
}
