import * as React from "react"

import { Icon } from "@/components/forge/icon"
import { Avatar, AvatarFallback } from "@/components/ui/avatar"
import { Button } from "@/components/ui/button"
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command"
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover"
import { Skeleton } from "@/components/ui/skeleton"
import { userName, users, type User } from "@/lib/users"

/** A small avatar with the user's initials. */
export function UserAvatar({ user }: { user: User }) {
  return (
    <Avatar size="sm" aria-hidden="true">
      <AvatarFallback className="text-3xs">
        {(user.first_name[0] ?? "") + (user.last_name[0] ?? "")}
      </AvatarFallback>
    </Avatar>
  )
}

function UserLabel({ user }: { user: User }) {
  return (
    <span className="flex min-w-0 flex-1 items-center gap-2">
      <UserAvatar user={user} />
      <span className="flex min-w-0 flex-col">
        <span className="truncate font-medium text-foreground">
          {userName(user)}
        </span>
        <span className="truncate text-2xs text-muted-foreground">
          {user.email}
        </span>
      </span>
    </span>
  )
}

type UserPickerProps = {
  /** For the field's label. */
  id?: string
  /** The picked users' IDs. */
  value: string[]
  onValueChange: (value: string[]) => void
  /** Names what's being picked, e.g. "Add administrators". */
  placeholder?: string
}

/**
 * Picks several people from the user directory: a searchable menu, with the
 * people picked so far listed underneath, each removable.
 */
export function UserPicker({
  id,
  value,
  onValueChange,
  placeholder = "Add people",
}: UserPickerProps) {
  const list = users.useList()
  const [open, setOpen] = React.useState(false)
  const everyone = list.data ?? []
  const picked = value
    .map((userId) => everyone.find((user) => user.id === userId))
    .filter((user) => user !== undefined)

  const toggle = (userId: string) =>
    onValueChange(
      value.includes(userId)
        ? value.filter((other) => other !== userId)
        : [...value, userId]
    )

  return (
    <div className="flex flex-col gap-2">
      <Popover open={open} onOpenChange={setOpen}>
        <PopoverTrigger
          render={
            <Button
              id={id}
              type="button"
              variant="outline"
              className="w-full justify-between font-normal text-muted-foreground"
            />
          }
        >
          {placeholder}
          <Icon icon="down" data-icon="inline-end" />
        </PopoverTrigger>
        <PopoverContent align="start" className="w-(--anchor-width) gap-0 p-0">
          <Command>
            <CommandInput placeholder="Search by name, email or MS ID…" />
            <CommandList>
              {list.isPending ? (
                <div className="flex flex-col gap-2 p-2" aria-busy="true">
                  <Skeleton className="h-7" />
                  <Skeleton className="h-7" />
                </div>
              ) : list.error ? (
                <div className="flex flex-col items-center gap-2 py-5 text-center text-xs text-muted-foreground">
                  Couldn't load the users.
                  <Button
                    type="button"
                    variant="outline"
                    size="xs"
                    onClick={() => void list.refetch()}
                  >
                    Retry
                  </Button>
                </div>
              ) : (
                <>
                  <CommandEmpty>
                    {everyone.length === 0
                      ? "No users yet. Add people on the Users page."
                      : "No one matches."}
                  </CommandEmpty>
                  <CommandGroup>
                    {everyone.map((user) => (
                      <CommandItem
                        key={user.id}
                        // Emails are unique, so this is too; search reads it.
                        value={`${userName(user)} ${user.email} ${user.msid}`}
                        data-checked={value.includes(user.id)}
                        onSelect={() => toggle(user.id)}
                      >
                        <UserLabel user={user} />
                      </CommandItem>
                    ))}
                  </CommandGroup>
                </>
              )}
            </CommandList>
          </Command>
        </PopoverContent>
      </Popover>
      {picked.length > 0 && (
        <ul
          aria-label="Picked"
          className="flex flex-col divide-y rounded-(--radius-control) border"
        >
          {picked.map((user) => (
            <li
              key={user.id}
              className="flex items-center gap-2 py-1.5 pr-1.5 pl-2 text-xs"
            >
              <UserLabel user={user} />
              <Button
                type="button"
                variant="ghost"
                size="icon-xs"
                aria-label={`Remove ${userName(user)}`}
                onClick={() => toggle(user.id)}
              >
                <Icon icon="close" />
              </Button>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
