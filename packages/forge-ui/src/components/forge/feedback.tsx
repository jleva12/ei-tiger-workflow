import * as React from "react"
import { cn } from "cn"

import {
  Alert,
  AlertAction,
  AlertDescription,
  AlertTitle,
} from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Icon } from "./icon"
import type { IconProp } from "./icons"

/**
 * Recoverable error callout: soft red surface, an info glyph, a title, the
 * reason and an optional retry action. Use it for connection failures and
 * inline form or action errors.
 */
function ErrorCallout({
  title,
  action,
  icon = "info",
  className,
  children,
}: {
  title?: React.ReactNode
  action?: React.ReactNode
  icon?: IconProp
  className?: string
  children?: React.ReactNode
}) {
  return (
    <Alert
      variant="error"
      className={cn(
        "gap-x-[13px] px-[15px] py-3 text-xs/[1.6] wrap-anywhere *:[svg]:translate-y-px",
        action && "items-center pr-24",
        className
      )}
    >
      <Icon icon={icon} />
      {title && <AlertTitle className="font-medium">{title}</AlertTitle>}
      {children && (
        <AlertDescription
          className={cn("text-xs/[1.6]", title && "text-2xs/[1.6]")}
        >
          {children}
        </AlertDescription>
      )}
      {action && (
        <AlertAction className="top-1/2 right-[15px] -translate-y-1/2">
          {action}
        </AlertAction>
      )}
    </Alert>
  )
}

/** Quiet banner noting a sample or read-only mode, with an exit link. */
function PreviewBanner({
  icon = "layers",
  label,
  explanation,
  action,
  onAction,
  className,
}: {
  icon?: IconProp
  label: React.ReactNode
  explanation?: React.ReactNode
  action?: React.ReactNode
  onAction?: () => void
  className?: string
}) {
  return (
    <div
      data-slot="preview-banner"
      className={cn(
        "flex items-center gap-[7px] px-0.5 pb-[15px] text-3xs text-muted-foreground @max-[600px]/shell:text-4xs",
        className
      )}
    >
      <Icon icon={icon} size={14} />
      <span>
        {label}
        {explanation && (
          <span className="text-subtle @max-[600px]/shell:hidden">
            {" "}
            · {explanation}
          </span>
        )}
      </span>
      {action && (
        <button
          type="button"
          onClick={onAction}
          className="ml-auto flex items-center gap-1 underline underline-offset-3"
        >
          {action}
          <Icon icon="external" size={13} />
        </button>
      )}
    </div>
  )
}

/** "N matching tasks · filters" summary with a clear action. */
function ActiveFilters({
  onClear,
  clearLabel = "Clear filters",
  className,
  children,
}: {
  onClear?: () => void
  clearLabel?: string
  className?: string
  children: React.ReactNode
}) {
  return (
    <div
      data-slot="active-filters"
      className={cn(
        "mb-4 flex items-center justify-between text-2xs text-muted-foreground",
        className
      )}
    >
      <span>{children}</span>
      <button
        type="button"
        onClick={onClear}
        className="flex items-center gap-[5px] transition-colors hover:text-foreground"
      >
        {clearLabel}
        <Icon icon="close" size={12} />
      </button>
    </div>
  )
}

/** Centered "load more" button with an explanatory caption. */
function LoadMore({
  label = "Load more",
  hint,
  className,
  ...props
}: React.ComponentProps<typeof Button> & {
  label?: React.ReactNode
  hint?: React.ReactNode
}) {
  return (
    <div
      data-slot="load-more"
      className={cn("my-5 flex flex-col items-center gap-2.5", className)}
    >
      <Button variant="outline" {...props}>
        {label}
      </Button>
      {hint && <span className="text-3xs text-subtle">{hint}</span>}
    </div>
  )
}

export { ErrorCallout, PreviewBanner, ActiveFilters, LoadMore }
