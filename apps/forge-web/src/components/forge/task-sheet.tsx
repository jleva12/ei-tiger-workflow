import * as React from "react"
import { cn } from "cn"

import {
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet"
import { Icon } from "./icon"
import type { IconProp } from "./icons"

/** A 560px right-hand sheet for creating and inspecting records. */
function TaskSheetContent({
  className,
  ...props
}: React.ComponentProps<typeof SheetContent>) {
  return (
    <SheetContent
      className={cn(
        "overflow-y-auto data-[side=right]:w-[min(560px,calc(100%-2*var(--sheet-inset)))] data-[side=right]:sm:max-w-[560px]",
        className
      )}
      {...props}
    />
  )
}

/** Spaced uppercase eyebrow, a large title and a short description. */
function TaskSheetHeader({
  eyebrow,
  eyebrowIcon = "task",
  title,
  description,
  className,
}: {
  eyebrow?: React.ReactNode
  eyebrowIcon?: IconProp
  title: React.ReactNode
  description?: React.ReactNode
  className?: string
}) {
  return (
    <SheetHeader
      className={cn(
        "gap-3 px-7 pt-[31px] pb-[25px] max-[600px]:px-[22px] max-[600px]:pt-[27px] max-[600px]:pb-[22px]",
        className
      )}
    >
      {eyebrow && (
        <div className="mb-[5px] flex items-center gap-[7px] text-4xs tracking-[.09em] text-subtle uppercase">
          <Icon icon={eyebrowIcon} />
          <span className="pr-[25px] wrap-anywhere">{eyebrow}</span>
        </div>
      )}
      <SheetTitle className="pr-[15px] text-[1.4375rem] leading-[1.35] font-medium tracking-[-.5px] wrap-anywhere">
        {title}
      </SheetTitle>
      {description && (
        <SheetDescription className="max-w-[390px] text-xs/[1.7]">
          {description}
        </SheetDescription>
      )}
    </SheetHeader>
  )
}

/**
 * Compact form body for sheets. Style its FieldGroup / Field / Input /
 * Textarea / Select / NativeSelect children at the workspace's 12px form
 * density.
 */
function TaskForm({ className, ...props }: React.ComponentProps<"form">) {
  return (
    <form
      data-slot="task-form"
      className={cn(
        "flex flex-1 flex-col gap-[23px] px-7 pb-[25px] max-[600px]:px-[22px]",
        "[&_[data-slot=field-description]]:text-2xs/[1.6] [&_[data-slot=field-group]]:gap-[23px] [&_[data-slot=field-label]]:text-xs [&_[data-slot=field-label]]:leading-normal [&_[data-slot=field]]:gap-[9px]",
        "[&_[data-slot=input]]:h-9 [&_[data-slot=input]]:text-xs [&_[data-slot=native-select-wrapper]]:w-full [&_[data-slot=native-select]]:h-9 [&_[data-slot=native-select]]:text-xs",
        // [data-size] outranks the trigger's own data-[size=…]:h-* height.
        "[&_[data-slot=select-trigger][data-size]]:h-9 [&_[data-slot=select-trigger][data-size]]:w-full [&_[data-slot=select-trigger][data-size]]:text-xs",
        "[&_[data-slot=textarea]]:min-h-[130px] [&_[data-slot=textarea]]:resize-y [&_[data-slot=textarea]]:p-3 [&_[data-slot=textarea]]:text-xs/[1.7]",
        className
      )}
      {...props}
    />
  )
}

/** Two equal columns for short paired fields. */
function FormColumns({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="form-columns"
      className={cn("grid grid-cols-2 gap-5", className)}
      {...props}
    />
  )
}

/** Bottom row: a quiet context hint on the left, actions on the right. */
function FormFooter({
  hint,
  hintIcon = "branch",
  className,
  children,
}: {
  hint?: React.ReactNode
  hintIcon?: IconProp
  className?: string
  children?: React.ReactNode
}) {
  return (
    <div
      data-slot="form-footer"
      className={cn(
        "mt-auto flex items-center justify-between gap-3.5 pt-6 *:data-[slot=button]:h-[35px] max-[600px]:items-start",
        className
      )}
    >
      {hint ? (
        <span className="flex items-center gap-[5px] text-3xs text-subtle max-[600px]:max-w-[145px] max-[600px]:leading-[1.6]">
          <Icon icon={hintIcon} size={14} />
          {hint}
        </span>
      ) : (
        <span />
      )}
      {children}
    </div>
  )
}

/** Label/value property list framed by hairlines. */
function DetailList({ className, ...props }: React.ComponentProps<"dl">) {
  return (
    <dl
      data-slot="detail-list"
      className={cn(
        "mx-7 flex flex-col gap-4 border-y py-5 max-[600px]:mx-[22px]",
        className
      )}
      {...props}
    />
  )
}

function DetailRow({
  label,
  className,
  children,
}: {
  label: React.ReactNode
  className?: string
  children: React.ReactNode
}) {
  return (
    <div className={cn("flex items-center gap-[15px] text-xs", className)}>
      <dt className="w-[110px] shrink-0 text-muted-foreground">{label}</dt>
      <dd className="min-w-0">{children}</dd>
    </div>
  )
}

/** A reusable plan/template entry with its agent roster. */
function PlanEntry({
  title,
  meta,
  description,
  className,
  children,
}: {
  title: React.ReactNode
  meta?: React.ReactNode
  description?: React.ReactNode
  className?: string
  children?: React.ReactNode
}) {
  return (
    <article data-slot="plan-entry" className={cn("border-t py-5", className)}>
      <div className="flex items-center justify-between gap-3">
        <h3 className="text-[0.9375rem] font-medium">{title}</h3>
        {meta && <span className="text-3xs text-subtle">{meta}</span>}
      </div>
      {description && (
        <p className="mt-2 mb-5 text-xs/[1.7] text-muted-foreground">
          {description}
        </p>
      )}
      {children}
    </article>
  )
}

function PlanAgent({
  name,
  detail,
  icon = "robot",
}: {
  name: React.ReactNode
  detail?: React.ReactNode
  icon?: IconProp
}) {
  return (
    <div data-slot="plan-agent" className="flex items-center gap-2.5 py-2.5">
      <Icon icon={icon} />
      <span>
        <strong className="text-xs font-medium">{name}</strong>
        {detail && (
          <small className="mt-1 block text-3xs text-muted-foreground">
            {detail}
          </small>
        )}
      </span>
    </div>
  )
}

export {
  TaskSheetContent,
  TaskSheetHeader,
  TaskForm,
  FormColumns,
  FormFooter,
  DetailList,
  DetailRow,
  PlanEntry,
  PlanAgent,
}
