import * as React from "react"
import { UnfoldMoreIcon } from "@hugeicons/core-free-icons"

import { Icon } from "@/components/forge/icon"
import { Avatar, AvatarFallback } from "@/components/ui/avatar"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { Skeleton } from "@/components/ui/skeleton"
import { toast } from "@/components/ui/toast"
import { useOpenSettings } from "@/app/settings/settings"
import { useMe, userName, type Me } from "@/lib/users"

/** Pinned to the bottom of the sub nav, below its sections. */
function Frame({ children }: { children: React.ReactNode }) {
  return (
    <div data-slot="nav-user" className="mt-auto border-t p-2">
      {children}
    </div>
  )
}

const rowClass =
  "flex w-full min-w-0 items-center gap-2.5 rounded-(--radius-item) px-2 py-1.5 text-left"

function Initials({ me }: { me: Me }) {
  const user = me.user
  const letters = user
    ? (user.first_name[0] ?? "") + (user.last_name[0] ?? "")
    : me.subject[0]
  return (
    <Avatar aria-hidden="true">
      <AvatarFallback className="text-2xs font-medium text-foreground">
        {letters?.toUpperCase()}
      </AvatarFallback>
    </Avatar>
  )
}

function Identity({ me }: { me: Me }) {
  return (
    <>
      <Initials me={me} />
      <span className="flex min-w-0 flex-1 flex-col">
        <span className="truncate text-xs font-medium text-foreground">
          {me.user ? userName(me.user) : "Signed in"}
        </span>
        <span className="truncate text-2xs text-muted-foreground">
          {me.user?.email ?? me.subject}
        </span>
      </span>
    </>
  )
}

/**
 * The signed-in user at the bottom of the sub nav: who they are, and a menu
 * for their settings. Loads `GET /me`; without a valid bearer token it says
 * they aren't signed in.
 */
export function NavUser() {
  const me = useMe()
  const openSettings = useOpenSettings()

  if (me.isPending) {
    return (
      <Frame>
        <div className={rowClass} aria-busy="true">
          <Skeleton className="size-8 shrink-0 rounded-full" />
          <span className="flex flex-1 flex-col gap-1.5">
            <Skeleton className="h-3 w-24" />
            <Skeleton className="h-2.5 w-32" />
          </span>
        </div>
      </Frame>
    )
  }

  if (me.error) {
    const signedOut = me.error.status === 401
    return (
      <Frame>
        <div className={rowClass} role="status">
          <Avatar aria-hidden="true">
            <AvatarFallback>
              <Icon icon="warning" size={15} />
            </AvatarFallback>
          </Avatar>
          <span className="flex min-w-0 flex-1 flex-col">
            <span className="truncate text-xs font-medium text-foreground">
              {signedOut ? "Not signed in" : "Couldn't load your account"}
            </span>
            <span className="truncate text-2xs text-muted-foreground">
              {signedOut ? "Run make web-token" : me.error.message}
            </span>
          </span>
        </div>
      </Frame>
    )
  }

  const data = me.data
  const copyId = () =>
    void navigator.clipboard.writeText(data.subject).then(
      () => toast.add({ title: "User ID copied", type: "success" }),
      () => toast.add({ title: "Couldn't copy the user ID", type: "error" })
    )

  return (
    <Frame>
      <DropdownMenu>
        <DropdownMenuTrigger
          render={
            <button
              type="button"
              className={`${rowClass} hover:bg-accent data-popup-open:bg-accent`}
            />
          }
        >
          <Identity me={data} />
          <Icon
            icon={UnfoldMoreIcon}
            size={15}
            className="shrink-0 text-muted-foreground"
          />
        </DropdownMenuTrigger>
        <DropdownMenuContent
          side="top"
          align="start"
          sideOffset={6}
          className="w-(--anchor-width) min-w-56"
        >
          <DropdownMenuGroup>
            <div className="flex items-center gap-2.5 px-2 py-1.5">
              <Identity me={data} />
            </div>
            {data.user && (
              <p className="px-2 pb-1.5 text-2xs text-subtle">
                MS ID <span className="font-mono">{data.user.msid}</span>
              </p>
            )}
          </DropdownMenuGroup>
          <DropdownMenuSeparator />
          <DropdownMenuGroup>
            <DropdownMenuItem onClick={() => openSettings("display")}>
              <Icon icon="settings" />
              Settings
            </DropdownMenuItem>
          </DropdownMenuGroup>
          <DropdownMenuSeparator />
          <DropdownMenuItem onClick={copyId}>
            <Icon icon="copy" />
            Copy user ID
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
    </Frame>
  )
}
