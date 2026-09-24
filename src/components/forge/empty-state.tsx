import * as React from "react"
import { cn } from "cn"

import {
  Empty,
  EmptyContent,
  EmptyDescription,
  EmptyHeader,
  EmptyMedia,
  EmptyTitle,
} from "@/components/ui/empty"
import { Icon } from "./icon"
import type { IconProp } from "./icons"
import { Illustration, type IllustrationName } from "./illustration"

/**
 * The workspace's paper illustration with the spacing `EmptyWorkspace`
 * expects below it. Defaults to the checklist; pass `name` for another state.
 */
function EmptyIllustration({
  name = "tasks",
  className,
}: {
  name?: IllustrationName
  className?: string
}) {
  return <Illustration name={name} className={cn("mb-[26px]", className)} />
}

type WorkflowStep = { icon: IconProp; label: string }

/** "Describe → Implement → Review → Done" style process hint. */
function WorkflowSteps({
  steps,
  className,
}: {
  steps: WorkflowStep[]
  className?: string
}) {
  return (
    <div
      data-slot="workflow-steps"
      className={cn(
        "flex flex-wrap items-center justify-center gap-[13px] text-3xs text-subtle @max-[800px]/shell:gap-2 @max-[600px]/shell:gap-[7px] @max-[600px]/shell:text-4xs",
        className
      )}
    >
      {steps.map((step, index) => (
        <React.Fragment key={step.label}>
          {index > 0 && <Icon icon="right" size={12} className="opacity-50" />}
          <span className="flex items-center gap-1.5 @max-[600px]/shell:gap-1">
            <Icon icon={step.icon} size={14} />
            {step.label}
          </span>
        </React.Fragment>
      ))}
    </div>
  )
}

/**
 * The full-page empty state: illustration, spaced eyebrow, large title,
 * short description, actions and an optional workflow hint.
 */
function EmptyWorkspace({
  eyebrow,
  title,
  description,
  actions,
  steps,
  illustration = <EmptyIllustration />,
  className,
}: {
  eyebrow?: React.ReactNode
  title: React.ReactNode
  description?: React.ReactNode
  actions?: React.ReactNode
  steps?: WorkflowStep[]
  illustration?: React.ReactNode
  className?: string
}) {
  return (
    <Empty
      className={cn(
        "h-full min-h-[480px] gap-0 rounded-none border-0 px-5 pt-0 pb-[60px] @max-[800px]/shell:min-h-[400px] @max-[600px]/shell:min-h-[490px] @max-[600px]/shell:px-0 @max-[600px]/shell:pt-[25px] @max-[600px]/shell:pb-[35px]",
        className
      )}
    >
      <EmptyHeader className="max-w-none gap-0">
        <EmptyMedia className="mb-0">{illustration}</EmptyMedia>
        {eyebrow && (
          <span className="mb-[13px] text-4xs tracking-[.15em] text-subtle uppercase">
            {eyebrow}
          </span>
        )}
        <EmptyTitle className="mb-[11px] font-sans text-[1.4375rem] tracking-[-.65px] @max-[600px]/shell:text-[1.3125rem]">
          {title}
        </EmptyTitle>
        {description && (
          <EmptyDescription className="max-w-[350px] text-xs/[1.8] @max-[600px]/shell:max-w-[300px]">
            {description}
          </EmptyDescription>
        )}
      </EmptyHeader>
      {actions && (
        <EmptyContent className="mt-[23px] mb-[47px] w-auto max-w-none flex-row justify-center gap-2.5 *:data-[slot=button]:h-[34px] @max-[600px]/shell:mb-[30px] @max-[600px]/shell:flex-col @max-[600px]/shell:gap-2">
          {actions}
        </EmptyContent>
      )}
      {steps && <WorkflowSteps steps={steps} />}
    </Empty>
  )
}

/**
 * Compact empty state for panels and sheets: an icon (or a small paper
 * illustration) and one line of text.
 */
function PanelEmpty({
  icon = "activity",
  illustration,
  className,
  children,
}: {
  icon?: IconProp
  /** Draws this paper illustration instead of the icon. */
  illustration?: IllustrationName
  className?: string
  children: React.ReactNode
}) {
  return (
    <Empty
      className={cn(
        "flex-none gap-[15px] rounded-none border-0 px-5 py-[55px] text-xs text-subtle",
        className
      )}
    >
      <EmptyMedia className="mb-0">
        {illustration ? (
          <Illustration name={illustration} size="sm" />
        ) : (
          <Icon icon={icon} size={24} />
        )}
      </EmptyMedia>
      <EmptyDescription className="text-xs text-subtle">
        {children}
      </EmptyDescription>
    </Empty>
  )
}

/** Centered empty state for a detail page tab, with optional body. */
function PageEmpty({
  icon = "clock",
  illustration,
  title,
  description,
  className,
  children,
}: {
  icon?: IconProp
  /** Draws this paper illustration instead of the icon. */
  illustration?: IllustrationName
  title: React.ReactNode
  description?: React.ReactNode
  className?: string
  children?: React.ReactNode
}) {
  return (
    <Empty
      className={cn(
        "min-h-[360px] gap-3 rounded-none border-0 p-10 text-muted-foreground",
        className
      )}
    >
      <EmptyHeader className="max-w-[480px] gap-3">
        <EmptyMedia className={cn("mb-0", illustration && "mb-2")}>
          {illustration ? (
            <Illustration name={illustration} />
          ) : (
            <Icon icon={icon} size={28} />
          )}
        </EmptyMedia>
        <EmptyTitle className="font-sans text-lg font-medium tracking-normal text-foreground">
          {title}
        </EmptyTitle>
        {description && (
          <EmptyDescription className="text-sm/[1.65]">
            {description}
          </EmptyDescription>
        )}
      </EmptyHeader>
      {children}
    </Empty>
  )
}

export {
  EmptyWorkspace,
  EmptyIllustration,
  WorkflowSteps,
  PanelEmpty,
  PageEmpty,
  type WorkflowStep,
}
