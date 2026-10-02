import * as React from "react"
import { cn } from "cn"

import { Button } from "@/components/ui/button"
import { Icon } from "@/components/forge/icon"
import type { IconProp } from "@/components/forge/icons"

/** A demo page: title, lede and a stack of specimens. */
export function SectionPage({
  icon,
  eyebrow,
  title,
  description,
  children,
}: {
  icon?: IconProp
  eyebrow: string
  title: string
  description: React.ReactNode
  children: React.ReactNode
}) {
  return (
    <div className="mx-auto max-w-[1120px] px-2 pt-2 pb-16 @max-[600px]/shell:px-0">
      <header className="mb-9 border-b pb-7">
        <div className="mb-3 flex items-center gap-[7px] text-4xs tracking-[.15em] text-subtle uppercase">
          {icon && <Icon icon={icon} size={13} />}
          {eyebrow}
        </div>
        <h1 className="mb-2.5 text-[1.75rem] leading-tight font-[550] tracking-[-.8px]">
          {title}
        </h1>
        <p className="max-w-[68ch] text-sm/[1.65] text-muted-foreground">
          {description}
        </p>
      </header>
      <div className="flex flex-col gap-10">{children}</div>
    </div>
  )
}

/** One labelled example with an optional usage snippet. */
export function Specimen({
  title,
  description,
  code,
  variant = "padded",
  className,
  children,
}: {
  title: string
  description?: React.ReactNode
  code?: string
  variant?: "padded" | "flush" | "canvas"
  className?: string
  children: React.ReactNode
}) {
  const [showCode, setShowCode] = React.useState(false)
  return (
    <section className="min-w-0">
      <div className="mb-3 flex items-end justify-between gap-4">
        <div className="min-w-0">
          <h2 className="text-sm font-[550]">{title}</h2>
          {description && (
            <p className="mt-1 max-w-[80ch] text-xs/[1.6] text-muted-foreground">
              {description}
            </p>
          )}
        </div>
        {code && (
          <Button
            variant="ghost"
            size="xs"
            aria-pressed={showCode}
            onClick={() => setShowCode((value) => !value)}
            className="shrink-0 text-muted-foreground"
          >
            <Icon icon="code" data-icon="inline-start" />
            {showCode ? "Hide code" : "Code"}
          </Button>
        )}
      </div>
      <div
        className={cn(
          "min-w-0 overflow-hidden rounded-(--radius-band) border bg-background",
          variant === "padded" && "p-6 @max-[600px]/shell:p-4",
          variant === "canvas" &&
            "bg-[color-mix(in_oklch,var(--muted)_40%,var(--background))] p-6 @max-[600px]/shell:p-4",
          className
        )}
      >
        {children}
      </div>
      {code && showCode && <CodeBlock code={code} />}
    </section>
  )
}

export function CodeBlock({ code }: { code: string }) {
  const [copied, setCopied] = React.useState(false)
  return (
    <div className="relative mt-2 overflow-hidden rounded-(--radius-band) border border-console-border bg-console scrollbar-dark">
      <Button
        variant="ghost"
        size="xs"
        className="absolute top-2 right-2 text-console-text hover:bg-console-control hover:text-console-foreground dark:hover:bg-console-control"
        onClick={() => {
          void navigator.clipboard.writeText(code.trim()).then(() => {
            setCopied(true)
            setTimeout(() => setCopied(false), 1400)
          })
        }}
      >
        <Icon icon={copied ? "check" : "copy"} data-icon="inline-start" />
        {copied ? "Copied" : "Copy"}
      </Button>
      <pre
        tabIndex={0}
        className="max-h-[420px] overflow-auto px-4 py-3.5 pr-20 font-mono text-xs/[1.7] text-console-foreground"
      >
        {code.trim()}
      </pre>
    </div>
  )
}

/** Wrapping row of specimens with consistent spacing. */
export function Row({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      className={cn("flex flex-wrap items-center gap-3", className)}
      {...props}
    />
  )
}

/** Small uppercase caption above a group inside a specimen. */
export function Caption({ children }: { children: React.ReactNode }) {
  return (
    <div className="mb-3 text-4xs tracking-[.12em] text-subtle uppercase">
      {children}
    </div>
  )
}
