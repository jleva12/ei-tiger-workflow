import * as React from "react"
import { cn } from "cn"

import { Button } from "@/components/ui/button"
import { TabsList, TabsTrigger } from "@/components/ui/tabs"
import { Icon } from "./icon"
import type { IconProp } from "./icons"
import { Chip, ConnectionDot } from "./status"

/*
 * Detail pages use the shared 14px UI size (--text-ui) for prose, logs and
 * primary controls; 13px for supporting text, 12px captions, 18px section
 * headings and a 26px title. Prose stays within 75ch.
 */

/** Scrolling container for a record's detail page. */
function TaskPage({ className, ...props }: React.ComponentProps<"section">) {
  return (
    <section
      data-slot="task-page"
      tabIndex={-1}
      className={cn(
        "relative min-h-0 flex-1 overflow-auto bg-background text-sm/[1.6] text-foreground outline-none [font-kerning:normal] [&_[data-slot=button]]:text-sm [&_code]:[font-variant-ligatures:none] [&_pre]:[font-variant-ligatures:none]",
        className
      )}
      {...props}
    />
  )
}

function TaskPageHeader({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="task-page-header"
      className={cn(
        "px-[30px] pt-[18px] pb-6 @max-[900px]/shell:px-5 @max-[900px]/shell:pt-4 @max-[900px]/shell:pb-[22px] @max-[600px]/shell:px-[15px] @max-[600px]/shell:pt-[13px] @max-[600px]/shell:pb-5",
        className
      )}
      {...props}
    />
  )
}

/** "← All tasks / ID" trail above the title, with an optional mode label. */
function TaskPageEyebrow({
  backLabel = "All tasks",
  onBack,
  reference,
  label,
  className,
}: {
  backLabel?: React.ReactNode
  onBack?: () => void
  reference?: React.ReactNode
  label?: React.ReactNode
  className?: string
}) {
  return (
    <div
      data-slot="task-page-eyebrow"
      className={cn(
        "mb-[19px] flex items-center gap-3 text-[0.8125rem] text-muted-foreground @max-[600px]/shell:mb-3 @max-[600px]/shell:gap-2",
        className
      )}
    >
      <Button
        variant="ghost"
        size="sm"
        onClick={onBack}
        className="-ml-[9px] text-sm text-muted-foreground"
      >
        <Icon icon="left" size={14} data-icon="inline-start" />
        {backLabel}
      </Button>
      {reference && (
        <>
          <span aria-hidden="true">/</span>
          <code className="text-[0.8125rem]">{reference}</code>
        </>
      )}
      {label && (
        <span className="ml-auto rounded-(--radius-item) bg-muted px-2 py-1 @max-[600px]/shell:text-xs">
          {label}
        </span>
      )}
    </div>
  )
}

/** Title + meta on the left, actions on the right; stacks on narrow shells. */
function TaskTitleRow({
  title,
  meta,
  actions,
  className,
  children,
}: {
  title: React.ReactNode
  meta?: React.ReactNode
  actions?: React.ReactNode
  className?: string
  children?: React.ReactNode
}) {
  return (
    <div
      data-slot="task-title-row"
      className={cn(
        "flex items-start justify-between gap-6 @max-[1250px]/shell:flex-col @max-[1250px]/shell:gap-[15px]",
        className
      )}
    >
      <div className="min-w-0">
        <h1 className="mb-3.5 text-[1.625rem] leading-[1.3] font-[550] tracking-[-.7px] wrap-anywhere outline-none @max-[600px]/shell:text-[1.375rem]">
          {title}
        </h1>
        {meta && (
          <div className="flex flex-wrap items-center gap-[17px] text-sm text-muted-foreground @max-[600px]/shell:gap-[11px] @max-[600px]/shell:text-xs">
            {meta}
          </div>
        )}
        {children}
      </div>
      {actions && (
        <div className="flex shrink-0 flex-wrap items-center gap-2 pt-0.5 *:data-[slot=button]:h-8 *:data-[slot=button]:rounded-(--radius-soft)">
          {actions}
        </div>
      )}
    </div>
  )
}

/** Icon + text meta item for the title row. */
function TaskMeta({
  icon,
  className,
  children,
}: {
  icon?: IconProp
  className?: string
  children: React.ReactNode
}) {
  return (
    <span className={cn("inline-flex items-center gap-1.5", className)}>
      {icon && <Icon icon={icon} size={14} />}
      {children}
    </span>
  )
}

/** Tab bar row under the page header: line tabs left, run controls right. */
function TaskTabBar({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="task-tab-bar"
      className={cn(
        "flex min-h-[49px] flex-wrap items-center justify-between gap-3 border-b px-[30px] @max-[900px]/shell:gap-0 @max-[900px]/shell:px-5 @max-[600px]/shell:px-[15px]",
        className
      )}
      {...props}
    />
  )
}

function TaskTabsList({
  className,
  ...props
}: React.ComponentProps<typeof TabsList>) {
  return (
    <TabsList
      variant="line"
      className={cn(
        "h-12! max-w-full justify-start gap-[22px] overflow-x-auto p-0 @max-[600px]/shell:gap-4",
        className
      )}
      {...props}
    />
  )
}

/** Underlined tab with an icon and an optional live indicator. */
function TaskTabsTrigger({
  icon,
  live = false,
  className,
  children,
  ...props
}: React.ComponentProps<typeof TabsTrigger> & {
  icon?: IconProp
  live?: boolean
}) {
  return (
    <TabsTrigger
      className={cn(
        "h-12 flex-none gap-[7px] rounded-none px-px text-sm font-medium after:bottom-0 group-data-horizontal/tabs:after:bottom-0 [&_svg:not([class*='size-'])]:size-[15px] @max-[600px]/shell:[&>svg]:hidden",
        className
      )}
      {...props}
    >
      {icon && <Icon icon={icon} />}
      {children}
      {live && (
        <span
          aria-label="Live"
          className="ml-0.5 size-[5px] rounded-full bg-signal-running"
        />
      )}
    </TabsTrigger>
  )
}

/** "● Live" / "● Reconnecting" connection label. */
function LiveLabel({
  online = true,
  className,
  children,
}: {
  online?: boolean
  className?: string
  children: React.ReactNode
}) {
  return (
    <span
      role="status"
      className={cn(
        "flex items-center gap-1.5 text-xs whitespace-nowrap text-muted-foreground",
        className
      )}
    >
      <ConnectionDot online={online} />
      {children}
    </span>
  )
}

/** Main column plus a 260px aside with hairline separation. */
function SubmissionLayout({
  aside,
  className,
  children,
}: {
  aside?: React.ReactNode
  className?: string
  children: React.ReactNode
}) {
  return (
    <div
      data-slot="submission-layout"
      className={cn(
        "grid max-w-[1500px] grid-cols-[minmax(0,1fr)_260px] gap-[46px] px-[30px] pt-7 pb-[60px] @max-[1250px]/shell:grid-cols-[minmax(0,1fr)_215px] @max-[1250px]/shell:gap-[25px] @max-[900px]/shell:grid-cols-1 @max-[900px]/shell:px-5 @max-[900px]/shell:py-6 @max-[600px]/shell:px-[15px] @max-[600px]/shell:py-5",
        className
      )}
    >
      <div className="min-w-0">{children}</div>
      {aside && (
        <aside className="border-l pl-[27px] @max-[1250px]/shell:pl-5 @max-[900px]/shell:border-t @max-[900px]/shell:border-l-0 @max-[900px]/shell:px-0 @max-[900px]/shell:pt-[25px]">
          {aside}
        </aside>
      )}
    </div>
  )
}

/** Titled aside group with a definition list of facts. */
function AsideSection({
  title,
  className,
  children,
}: {
  title: React.ReactNode
  className?: string
  children: React.ReactNode
}) {
  return (
    <section
      className={cn(
        "[&+&]:mt-[30px] [&+&]:border-t [&+&]:pt-[26px]",
        className
      )}
    >
      <h3 className="mb-[19px] text-lg font-[550]">{title}</h3>
      <dl>{children}</dl>
    </section>
  )
}

function AsideFact({
  label,
  className,
  children,
}: {
  label: React.ReactNode
  className?: string
  children: React.ReactNode
}) {
  return (
    <>
      <dt className="mt-[17px] mb-[5px] text-xs text-muted-foreground first:mt-0">
        {label}
      </dt>
      <dd
        className={cn(
          "flex items-center gap-1.5 text-sm break-words wrap-anywhere",
          className
        )}
      >
        {children}
      </dd>
    </>
  )
}

/** A page section with an 18px heading and optional trailing meta. */
function PageSection({
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
    <section data-slot="page-section" className={cn("mb-[34px]", className)}>
      <div className="flex items-center justify-between gap-4">
        <h2 className="mb-3 text-lg font-[550]">{title}</h2>
        {meta && (
          <span className="text-[0.8125rem] text-muted-foreground">{meta}</span>
        )}
      </div>
      {description && (
        <p className="mb-[18px] max-w-[75ch] text-sm/[1.65] text-muted-foreground">
          {description}
        </p>
      )}
      {children}
    </section>
  )
}

/** Long-form user text: pre-wrapped, 75ch measure, slightly softened. */
function Prose({ className, ...props }: React.ComponentProps<"p">) {
  return (
    <p
      className={cn(
        "max-w-[75ch] text-sm/[1.7] wrap-anywhere whitespace-pre-wrap text-foreground/80",
        className
      )}
      {...props}
    />
  )
}

/** Warm notice for work that is queued or waiting on something. */
function WaitingNotice({
  icon = "clock",
  title,
  state,
  className,
  children,
}: {
  icon?: IconProp
  title: React.ReactNode
  state?: React.ReactNode
  className?: string
  children?: React.ReactNode
}) {
  return (
    <div
      data-slot="waiting-notice"
      className={cn(
        "mb-[34px] flex items-center gap-3.5 rounded-(--radius-card) border border-notice-border bg-notice-surface p-5 @max-[600px]/shell:items-start @max-[600px]/shell:p-[15px]",
        className
      )}
    >
      <span className="grid size-10 shrink-0 place-items-center rounded-full border border-notice-border bg-notice-icon-surface text-notice-accent">
        <Icon icon={icon} />
      </span>
      <div>
        <h2 className="mb-[5px] text-lg font-[550]">{title}</h2>
        {children && (
          <p className="max-w-[500px] text-sm/[1.6] text-notice-foreground">
            {children}
          </p>
        )}
      </div>
      {state && (
        <Chip tone="notice" className="ml-auto @max-[600px]/shell:hidden">
          {state}
        </Chip>
      )}
    </div>
  )
}

/** Numbered vertical timeline of workflow steps. */
function ExecutionPlan({ className, ...props }: React.ComponentProps<"ol">) {
  return (
    <ol
      data-slot="execution-plan"
      className={cn("mt-5 list-none p-0", className)}
      {...props}
    />
  )
}

function ExecutionStep({
  step,
  name,
  role,
  status,
  detail,
  completed = false,
  className,
  children,
}: {
  step: React.ReactNode
  name: React.ReactNode
  role?: React.ReactNode
  status?: React.ReactNode
  detail?: React.ReactNode
  completed?: boolean
  className?: string
  children?: React.ReactNode
}) {
  return (
    <li
      data-slot="execution-step"
      className={cn(
        "relative flex gap-4 pb-[27px] not-last:before:absolute not-last:before:top-[33px] not-last:before:bottom-0 not-last:before:left-[15px] not-last:before:border-l not-last:before:content-['']",
        className
      )}
    >
      <span
        className={cn(
          "relative grid size-[31px] shrink-0 place-items-center rounded-full border bg-background font-mono text-xs text-muted-foreground",
          completed &&
            "border-success-border bg-success-surface text-success-foreground"
        )}
      >
        {completed ? <Icon icon="check" size={14} /> : step}
      </span>
      <div className="min-w-0 flex-1 pt-[3px]">
        <div className="flex flex-wrap items-center gap-[9px]">
          <strong className="text-sm font-[550]">{name}</strong>
          {role && <Chip tone="outline">{role}</Chip>}
          {status && (
            <span className="ml-auto text-xs text-muted-foreground @max-[600px]/shell:ml-0">
              {status}
            </span>
          )}
        </div>
        {detail && (
          <p className="mt-[5px] mb-2 text-[0.8125rem] text-muted-foreground">
            {detail}
          </p>
        )}
        {children}
      </div>
    </li>
  )
}

/** Native disclosure with a muted summary and a boxed, scrollable body. */
function Disclosure({
  summary,
  defaultOpen,
  className,
  children,
}: {
  summary: React.ReactNode
  defaultOpen?: boolean
  className?: string
  children: React.ReactNode
}) {
  return (
    <details
      data-slot="disclosure"
      open={defaultOpen}
      className={cn("group/disclosure text-sm/[1.7]", className)}
    >
      <summary className="flex w-fit max-w-full cursor-pointer list-none items-center gap-1.5 text-muted-foreground hover:text-foreground [&::-webkit-details-marker]:hidden">
        <Icon
          icon="right"
          size={12}
          className="transition-transform duration-150 group-open/disclosure:rotate-90"
        />
        {summary}
      </summary>
      <div className="mt-3 max-h-[360px] overflow-auto rounded-(--radius-item) border bg-muted px-[15px] py-[13px] [&_pre]:font-mono [&_pre]:text-sm/[1.7] [&_pre]:wrap-anywhere [&_pre]:whitespace-pre-wrap">
        {children}
      </div>
    </details>
  )
}

/** Two-column label/value record with hairline rows. */
function RecordList({ className, ...props }: React.ComponentProps<"dl">) {
  return (
    <dl
      data-slot="record-list"
      className={cn("m-0 border-t", className)}
      {...props}
    />
  )
}

function RecordRow({
  label,
  className,
  children,
}: {
  label: React.ReactNode
  className?: string
  children: React.ReactNode
}) {
  return (
    <div
      className={cn(
        "grid grid-cols-[130px_minmax(0,1fr)] gap-[15px] border-b py-[11px] text-sm @max-[600px]/shell:grid-cols-[95px_minmax(0,1fr)]",
        className
      )}
    >
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="wrap-anywhere [&_code]:text-[0.8125rem]">{children}</dd>
    </div>
  )
}

export {
  TaskPage,
  TaskPageHeader,
  TaskPageEyebrow,
  TaskTitleRow,
  TaskMeta,
  TaskTabBar,
  TaskTabsList,
  TaskTabsTrigger,
  LiveLabel,
  SubmissionLayout,
  AsideSection,
  AsideFact,
  PageSection,
  Prose,
  WaitingNotice,
  ExecutionPlan,
  ExecutionStep,
  Disclosure,
  RecordList,
  RecordRow,
}
