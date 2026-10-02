import * as React from "react"
import { cn } from "cn"

import { Button } from "@/components/ui/button"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { Spinner } from "@/components/ui/spinner"
import { Icon } from "./icon"
import { SettingsRow } from "./settings-list"
import { ConnectionDot } from "./status"

/** Where an OAuth connection stands. */
type IntegrationStatus = "disconnected" | "connecting" | "connected" | "error"

/** A 36px tile that frames a brand logo, like an app icon. */
function IntegrationLogo({
  className,
  ...props
}: React.ComponentProps<"span">) {
  return (
    <span
      data-slot="integration-logo"
      className={cn(
        "flex size-9 shrink-0 items-center justify-center rounded-(--radius-card) border bg-background text-foreground shadow-(--shadow-raised) [&_svg]:size-5",
        className
      )}
      {...props}
    />
  )
}

/**
 * One third-party app on an integrations page: logo, name and what the
 * connection is for, its status, and the button for the next step —
 * Connect, a Manage menu once connected, or Reconnect after a failure.
 * The OAuth flow itself is yours: start it in `onConnect` (redirect or
 * popup) and pass the resulting `status` back in.
 */
function IntegrationRow({
  logo,
  name,
  description,
  status = "disconnected",
  account,
  detail,
  onConnect,
  onDisconnect,
  menu,
  className,
}: {
  /** A brand mark from `brand-logos` (or any 20px SVG); framed in a tile. */
  logo: React.ReactNode
  /** Product name, also used in button labels for screen readers. */
  name: string
  description?: React.ReactNode
  status?: IntegrationStatus
  /** The connected account, workspace or site, e.g. `josephleva`. */
  account?: React.ReactNode
  /**
   * A secondary line: when it was connected, or why it failed
   * ("Access was revoked on GitHub").
   */
  detail?: React.ReactNode
  /** Start (or restart) the OAuth flow. */
  onConnect?: () => void
  onDisconnect?: () => void
  /** Extra `DropdownMenuItem`s above Reconnect in the Manage menu. */
  menu?: React.ReactNode
  className?: string
}) {
  return (
    <SettingsRow
      data-status={status}
      aria-busy={status === "connecting" || undefined}
      className={className}
      media={<IntegrationLogo>{logo}</IntegrationLogo>}
      title={name}
      description={description}
      meta={
        <IntegrationMeta
          status={status}
          account={account}
          detail={detail}
          name={name}
        />
      }
      action={
        <IntegrationAction
          status={status}
          name={name}
          onConnect={onConnect}
          onDisconnect={onDisconnect}
          menu={menu}
        />
      }
    />
  )
}

function IntegrationMeta({
  status,
  name,
  account,
  detail,
}: {
  status: IntegrationStatus
  name: string
  account?: React.ReactNode
  detail?: React.ReactNode
}) {
  switch (status) {
    case "connected":
      return (
        <>
          <span className="flex items-center gap-1.5 font-medium text-foreground">
            <ConnectionDot />
            {account ?? "Connected"}
          </span>
          {detail && <span className="text-2xs text-subtle">{detail}</span>}
        </>
      )
    case "connecting":
      return <span>Waiting for {name}…</span>
    case "error":
      return (
        <>
          <span className="flex items-center gap-1.5 font-medium text-destructive">
            <Icon icon="warning" size={14} />
            Needs attention
          </span>
          {detail && <span className="text-2xs text-subtle">{detail}</span>}
        </>
      )
    default:
      return <span className="text-subtle">{detail ?? "Not connected"}</span>
  }
}

const actionWidth = "min-w-[104px]"

function IntegrationAction({
  status,
  name,
  onConnect,
  onDisconnect,
  menu,
}: {
  status: IntegrationStatus
  name: string
  onConnect?: () => void
  onDisconnect?: () => void
  menu?: React.ReactNode
}) {
  switch (status) {
    case "connecting":
      return (
        <Button variant="outline" size="sm" disabled className={actionWidth}>
          <Spinner data-icon="inline-start" aria-hidden="true" />
          Connecting
        </Button>
      )
    case "connected":
      return (
        <DropdownMenu>
          <DropdownMenuTrigger
            render={
              <Button
                variant="outline"
                size="sm"
                aria-label={`Manage ${name}`}
                className={actionWidth}
              />
            }
          >
            Manage
            <Icon icon="down" data-icon="inline-end" />
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="min-w-44">
            <DropdownMenuGroup>
              {menu}
              {onConnect && (
                <DropdownMenuItem onClick={onConnect}>
                  <Icon icon="refresh" />
                  Reconnect
                </DropdownMenuItem>
              )}
            </DropdownMenuGroup>
            {onDisconnect && (
              <>
                {(menu || onConnect) && <DropdownMenuSeparator />}
                <DropdownMenuItem variant="destructive" onClick={onDisconnect}>
                  <Icon icon="close" />
                  Disconnect
                </DropdownMenuItem>
              </>
            )}
          </DropdownMenuContent>
        </DropdownMenu>
      )
    case "error":
      return (
        <Button
          variant="outline"
          size="sm"
          aria-label={`Reconnect ${name}`}
          onClick={onConnect}
          className={actionWidth}
        >
          <Icon icon="refresh" data-icon="inline-start" />
          Reconnect
        </Button>
      )
    default:
      return (
        <Button
          variant="outline"
          size="sm"
          aria-label={`Connect ${name}`}
          onClick={onConnect}
          className={actionWidth}
        >
          Connect
          <Icon icon="external" data-icon="inline-end" />
        </Button>
      )
  }
}

export { IntegrationLogo, IntegrationRow, type IntegrationStatus }
