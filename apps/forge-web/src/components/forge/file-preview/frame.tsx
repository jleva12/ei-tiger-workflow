import * as React from "react"
import { cn } from "cn"
import {
  FitToScreenIcon,
  MinusSignIcon,
  PlusSignIcon,
} from "@hugeicons/core-free-icons"

import { Icon } from "@/components/forge/icon"
import type { IconProp } from "@/components/forge/icons"
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
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import type { PreviewKind } from "./kinds"
import { formatSize, KIND_ICONS } from "./preview"
import {
  isMac,
  MAX_ZOOM,
  MIN_ZOOM,
  PRESETS,
  stepZoom,
  type ZoomChange,
  type ZoomFit,
} from "./zoom"

/*
 * The viewer's chrome, shared by every renderer: a 40px toolbar over the
 * body, which is either the "desk" (pages on a muted surface) or a flat
 * surface for text and tables. Controls are compact ghost buttons with
 * tooltips, like the rest of the workspace's toolbars.
 */

/** Toolbar over a body that fills the rest. */
export function PreviewFrame({
  toolbar,
  toolbarEnd,
  desk = false,
  className,
  children,
  ...props
}: Omit<React.ComponentProps<"div">, "children"> & {
  /** Controls at the start of the toolbar. */
  toolbar?: React.ReactNode
  toolbarEnd?: React.ReactNode
  /** Pages on the muted desk, rather than a flat surface. */
  desk?: boolean
  children: React.ReactNode
}) {
  return (
    <div
      className="flex min-h-0 min-w-0 flex-1 flex-col"
      data-slot="preview-frame"
    >
      {(toolbar || toolbarEnd) && (
        <div
          role="toolbar"
          aria-label="Preview"
          className="flex h-10 shrink-0 [scrollbar-width:none] items-center gap-0.5 overflow-x-auto border-b bg-background px-2 text-xs"
        >
          {toolbar}
          {toolbarEnd && (
            <div className="ml-auto flex shrink-0 items-center gap-0.5 pl-2">
              {toolbarEnd}
            </div>
          )}
        </div>
      )}
      <div
        className={cn(
          "relative min-h-0 flex-1",
          desk ? "bg-muted" : "bg-background",
          className
        )}
        {...props}
      >
        {children}
      </div>
    </div>
  )
}

/** An icon button with its name as a tooltip. */
export function BarButton({
  label,
  icon,
  shortcut,
  pressed,
  className,
  ...props
}: Omit<React.ComponentProps<typeof Button>, "children"> & {
  label: string
  icon: IconProp
  /** Shown after the name in the tooltip, e.g. "⌘ +". */
  shortcut?: string
  /** A toggle's state. */
  pressed?: boolean
}) {
  return (
    <Tooltip>
      <TooltipTrigger
        render={
          <Button
            variant="ghost"
            size="icon-sm"
            aria-label={label}
            aria-pressed={pressed}
            className={cn(
              "text-muted-foreground hover:text-foreground",
              pressed && "bg-muted text-foreground",
              className
            )}
            {...props}
          />
        }
      >
        <Icon icon={icon} size={16} />
      </TooltipTrigger>
      <TooltipContent side="bottom">
        {label}
        {shortcut && <span className="ml-2 text-subtle">{shortcut}</span>}
      </TooltipContent>
    </Tooltip>
  )
}

/** A segmented switch between a file's views: "Preview | Source". */
export function ModeSwitch<T extends string>({
  label,
  value,
  onChange,
  options,
}: {
  label: string
  value: T
  onChange: (value: T) => void
  options: { value: T; label: string; icon?: IconProp; disabled?: boolean }[]
}) {
  return (
    <ToggleGroup
      aria-label={label}
      value={[value]}
      onValueChange={(next) => {
        if (next[0]) onChange(String(next[0]) as T)
      }}
      className="shrink-0 gap-0 rounded-(--radius-control) bg-muted p-[3px]"
    >
      {options.map((option) => (
        <ToggleGroupItem
          key={option.value}
          value={option.value}
          disabled={option.disabled}
          className="h-6 gap-1.5 px-2 text-xs text-muted-foreground hover:bg-muted aria-pressed:bg-background aria-pressed:text-foreground aria-pressed:shadow-(--shadow-raised)"
        >
          {option.icon && <Icon icon={option.icon} size={14} />}
          {option.label}
        </ToggleGroupItem>
      ))}
    </ToggleGroup>
  )
}

export function BarDivider() {
  return (
    <span aria-hidden="true" className="mx-1.5 h-4 w-px shrink-0 bg-border" />
  )
}

/** Text in the toolbar: a count, a label. */
export function BarText({ className, ...props }: React.ComponentProps<"span">) {
  return (
    <span
      className={cn(
        "shrink-0 px-1.5 text-xs whitespace-nowrap text-muted-foreground tabular-nums",
        className
      )}
      {...props}
    />
  )
}

/**
 * "‹ [3] / 24 ›": where you are in the pages (or slides), with a box to go
 * to one.
 */
export function PageControl({
  page,
  count,
  onPage,
  noun = "page",
}: {
  /** 1-based. */
  page: number
  count: number
  onPage: (page: number) => void
  noun?: string
}) {
  const [draft, setDraft] = React.useState<string>()
  const go = (next: number) => onPage(Math.min(Math.max(next, 1), count))
  const Noun = noun[0].toUpperCase() + noun.slice(1)
  return (
    <div className="flex shrink-0 items-center">
      <BarButton
        label={`Previous ${noun}`}
        icon="left"
        disabled={page <= 1}
        onClick={() => go(page - 1)}
      />
      <input
        aria-label={`${Noun} number`}
        inputMode="numeric"
        value={draft ?? String(page)}
        onFocus={(event) => event.target.select()}
        onChange={(event) => setDraft(event.target.value.replace(/\D/g, ""))}
        onBlur={() => setDraft(undefined)}
        onKeyDown={(event) => {
          if (event.key === "Enter" && draft) {
            go(Number(draft))
            setDraft(undefined)
            event.currentTarget.blur()
          }
          if (event.key === "Escape") {
            setDraft(undefined)
            event.currentTarget.blur()
          }
        }}
        className="h-6 w-9 rounded-(--radius-chip) border border-input bg-background text-center text-xs text-foreground tabular-nums outline-none focus-visible:border-ring"
        style={{ width: `${Math.max(2, String(count).length) + 1.5}ch` }}
      />
      <BarText>of {count.toLocaleString()}</BarText>
      <BarButton
        label={`Next ${noun}`}
        icon="right"
        disabled={page >= count}
        onClick={() => go(page + 1)}
      />
    </div>
  )
}

const FIT_LABELS: Record<ZoomFit, string> = {
  auto: "Automatic",
  width: "Fit width",
  page: "Fit page",
}

/** − [100%] +, the percentage a menu of fits and presets. */
export function ZoomControl({
  scale,
  fits = [],
  fit,
  onChange,
}: {
  /** The scale shown now, fitted or not (1 = 100%). */
  scale: number
  /** The fits this renderer offers. */
  fits?: ZoomFit[]
  /** The fit in force, if any. */
  fit?: ZoomFit | null
  onChange: (change: ZoomChange) => void
}) {
  const key = isMac() ? "⌘" : "Ctrl"
  return (
    <div className="flex shrink-0 items-center">
      <BarButton
        label="Zoom out"
        icon={MinusSignIcon}
        shortcut={`${key} −`}
        disabled={scale <= MIN_ZOOM + 0.001}
        onClick={() => onChange({ scale: stepZoom(scale, -1) })}
      />
      <DropdownMenu>
        <DropdownMenuTrigger
          render={
            <Button
              variant="ghost"
              size="sm"
              aria-label="Zoom level"
              className="h-7 min-w-14 px-1.5 text-xs text-foreground tabular-nums"
            />
          }
        >
          {Math.round(scale * 100)}%
        </DropdownMenuTrigger>
        <DropdownMenuContent align="center" className="min-w-36">
          {fits.length > 0 && (
            <>
              <DropdownMenuGroup>
                {fits.map((each) => (
                  <DropdownMenuItem
                    key={each}
                    onClick={() => onChange({ fit: each })}
                    className={cn(fit === each && "font-medium")}
                  >
                    <Icon icon={FitToScreenIcon} />
                    {FIT_LABELS[each]}
                    {fit === each && (
                      <Icon icon="check" className="ml-auto" size={14} />
                    )}
                  </DropdownMenuItem>
                ))}
              </DropdownMenuGroup>
              <DropdownMenuSeparator />
            </>
          )}
          <DropdownMenuGroup>
            {PRESETS.map((preset) => {
              const current = !fit && Math.abs(scale - preset) < 0.001
              return (
                <DropdownMenuItem
                  key={preset}
                  onClick={() => onChange({ scale: preset })}
                  className="tabular-nums"
                >
                  {preset * 100}%
                  {current && (
                    <Icon icon="check" className="ml-auto" size={14} />
                  )}
                </DropdownMenuItem>
              )
            })}
          </DropdownMenuGroup>
        </DropdownMenuContent>
      </DropdownMenu>
      <BarButton
        label="Zoom in"
        icon={PlusSignIcon}
        shortcut={`${key} +`}
        disabled={scale >= MAX_ZOOM - 0.001}
        onClick={() => onChange({ scale: stepZoom(scale, 1) })}
      />
    </div>
  )
}

/* -------------------------------------------------------------------------- */
/* States                                                                     */
/* -------------------------------------------------------------------------- */

/** The file's glyph on a muted tile, over a line or two of text. */
function Centered({
  icon,
  tone = "muted",
  children,
}: {
  icon: IconProp
  tone?: "muted" | "danger"
  children: React.ReactNode
}) {
  return (
    <div className="absolute inset-0 flex items-center justify-center overflow-auto p-6">
      <div className="flex max-w-[26rem] flex-col items-center gap-3 text-center">
        <span
          className={cn(
            "flex size-11 items-center justify-center rounded-(--radius-card)",
            tone === "danger"
              ? "bg-danger-surface text-danger-foreground"
              : "bg-background text-muted-foreground shadow-(--shadow-raised) ring-1 ring-border"
          )}
        >
          <Icon icon={icon} size={20} />
        </span>
        {children}
      </div>
    </div>
  )
}

/** While the file arrives (with how much has), or while it's read. */
export function PreviewLoading({
  kind,
  label = "Preparing the preview…",
  progress,
  size,
}: {
  kind: PreviewKind
  label?: string
  /** 0–1 of the file received, when known. */
  progress?: number
  /** The file's size, to say how much has arrived. */
  size?: number
}) {
  const percent =
    progress === undefined ? undefined : Math.round(Math.min(progress, 1) * 100)
  return (
    <Centered icon={KIND_ICONS[kind]}>
      <p
        className="flex items-center gap-2 text-sm text-muted-foreground"
        role="status"
      >
        {percent === undefined && <Spinner className="size-3.5" />}
        {label}
      </p>
      {percent !== undefined && (
        <div className="grid w-52 gap-1.5">
          <span
            role="progressbar"
            aria-label="Downloading"
            aria-valuenow={percent}
            aria-valuemin={0}
            aria-valuemax={100}
            className="relative h-1 overflow-hidden rounded-full bg-background ring-1 ring-border"
          >
            <span
              className="absolute inset-y-0 left-0 rounded-full bg-foreground transition-[width] duration-150 motion-reduce:transition-none"
              style={{ width: `${Math.max(percent, 3)}%` }}
            />
          </span>
          <span className="text-2xs text-subtle tabular-nums">
            {size
              ? `${formatSize(size * Math.min(progress ?? 0, 1))} of ${formatSize(size)}`
              : `${percent}%`}
          </span>
        </div>
      )}
    </Centered>
  )
}

/** Why there's nothing to show, and what to do instead. */
export function PreviewProblem({
  kind,
  title,
  description,
  tone = "muted",
  actions,
}: {
  kind: PreviewKind
  title: React.ReactNode
  description?: React.ReactNode
  tone?: "muted" | "danger"
  actions?: React.ReactNode
}) {
  return (
    <Centered
      icon={tone === "danger" ? "warning" : KIND_ICONS[kind]}
      tone={tone}
    >
      <p className="text-[0.9375rem] font-medium text-foreground">{title}</p>
      {description && (
        <p className="text-xs/relaxed text-muted-foreground">{description}</p>
      )}
      {actions && (
        <div className="mt-1 flex flex-wrap items-center justify-center gap-2">
          {actions}
        </div>
      )}
    </Centered>
  )
}

/** Download, as a problem's way out. */
export function DownloadButton({ onDownload }: { onDownload?: () => void }) {
  if (!onDownload) return null
  return (
    <Button variant="outline" size="sm" onClick={onDownload}>
      <Icon icon="download" data-icon="inline-start" />
      Download
    </Button>
  )
}
