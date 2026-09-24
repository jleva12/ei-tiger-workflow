import * as React from "react"
import { cn } from "cn"

/**
 * A settings page list: full-width rows divided by hairlines. It's its own
 * container (`@container/settings`), so rows stack their controls under the
 * text when the list is narrow — on a phone, in a sheet or in a dialog.
 */
function SettingsList({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="settings-list"
      className={cn(
        "@container/settings flex flex-col divide-y border-b",
        className
      )}
      {...props}
    />
  )
}

/**
 * One setting: optional media (an icon or logo tile), a title and a short
 * description on the left; a status or current value and an action on the
 * right.
 */
function SettingsRow({
  media,
  title,
  badge,
  description,
  meta,
  action,
  className,
  ...props
}: Omit<React.ComponentProps<"div">, "title"> & {
  /** Leading icon or logo tile, e.g. `IntegrationLogo`. */
  media?: React.ReactNode
  title: React.ReactNode
  /** Shown after the title, e.g. a `Chip`. */
  badge?: React.ReactNode
  description?: React.ReactNode
  /** Status or current value, right-aligned before the action. */
  meta?: React.ReactNode
  /** Usually one `Button variant="outline" size="sm"` or a menu trigger. */
  action?: React.ReactNode
}) {
  return (
    <div
      data-slot="settings-row"
      className={cn(
        "flex items-center gap-x-6 gap-y-3 py-[18px] @max-[560px]/settings:flex-wrap",
        className
      )}
      {...props}
    >
      <div className="flex min-w-0 flex-1 items-center gap-3.5 @max-[560px]/settings:basis-full">
        {media}
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-sm font-medium text-foreground">
            {title}
            {badge}
          </div>
          {description && (
            <p className="mt-1 text-xs/[1.6] text-muted-foreground">
              {description}
            </p>
          )}
        </div>
      </div>
      {(meta || action) && (
        <div
          data-slot="settings-row-end"
          className="flex shrink-0 items-center gap-4 @max-[560px]/settings:basis-full @max-[560px]/settings:justify-between"
        >
          {meta && (
            <div className="flex min-w-0 flex-col items-end gap-0.5 text-right text-xs text-muted-foreground @max-[560px]/settings:items-start @max-[560px]/settings:text-left">
              {meta}
            </div>
          )}
          {action}
        </div>
      )}
    </div>
  )
}

export { SettingsList, SettingsRow }
