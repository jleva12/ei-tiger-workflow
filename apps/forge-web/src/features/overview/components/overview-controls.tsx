import * as React from "react"
import { AiBrain01Icon, FlowSquareIcon } from "@hugeicons/core-free-icons"
import type { DateRange } from "react-day-picker"

import { Icon } from "@/components/forge/icon"
import type { IconProp } from "@/components/forge/icons"
import { Button } from "@/components/ui/button"
import { Calendar } from "@/components/ui/calendar"
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
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import {
  daysLabel,
  dayOf,
  DEFAULT_PERIOD,
  kindOf,
  OVERVIEW_PERIODS,
  SUBJECT_KINDS,
  useOrganizationOverview,
  type OverviewPeriod,
  type SubjectKind,
} from "@/features/overview/lib/overview"
import {
  spanOf,
  type OverviewView,
} from "@/features/overview/lib/overview-view"
import { KIND_ORDER } from "@/features/overview/lib/series"

/*
 * The overview's controls in the top bar: what it's focused on (everything,
 * one type, or one workflow or agent, picked from a list grouped by type, as
 * a row of the ledger picks it), and its period (the last 7, 30 or 90 days,
 * or days picked on a calendar, up to a year).
 */

/** The most days a picked period may have, as the API allows. */
const MAX_DAYS = 366

const KIND_ICONS: Record<SubjectKind, IconProp> = {
  workflow: FlowSquareIcon,
  agent: AiBrain01Icon,
  assistant: "sparkles",
}

type Change = (change: Partial<OverviewView>) => void

export function OverviewControls({
  organizationId,
  view,
  onView,
}: {
  organizationId: string
  view: OverviewView
  onView: Change
}) {
  return (
    <>
      <FocusPicker
        organizationId={organizationId}
        view={view}
        onView={onView}
      />
      <PeriodPicker view={view} onView={onView} />
    </>
  )
}

/* Focus ------------------------------------------------------------------- */

const ALL = "all"
const KIND = "kind:"

/**
 * Search by name only, as typed: the words in it, names starting with them
 * first. (cmdk's own matching is fuzzy, over each item's value too, and a
 * key like `workflow:ag_…` matches most of what's typed.)
 */
const byName = (_value: string, search: string, keywords?: string[]) => {
  const words = search.trim().toLowerCase()
  if (!words) return 1
  const names = (keywords ?? []).map((keyword) => keyword.toLowerCase())
  if (names.some((name) => name.startsWith(words))) return 1
  return names.some((name) => name.includes(words)) ? 0.5 : 0
}

function FocusPicker({
  organizationId,
  view,
  onView,
}: {
  organizationId: string
  view: OverviewView
  onView: Change
}) {
  const [open, setOpen] = React.useState(false)
  // The overview the page shows: its subjects, from the same query.
  const overview = useOrganizationOverview(organizationId, spanOf(view)).data
  const subjects = React.useMemo(
    () =>
      [...(overview?.subjects ?? [])].sort((a, b) =>
        a.name.localeCompare(b.name)
      ),
    [overview?.subjects]
  )
  const focused = view.focus
    ? subjects.find((subject) => subject.key === view.focus)
    : undefined
  const label = focused
    ? focused.name
    : view.only
      ? `All ${SUBJECT_KINDS[view.only].plural.toLowerCase()}`
      : "All workflows and agents"
  const current = focused?.key ?? (view.only ? `${KIND}${view.only}` : ALL)

  const pick = (value: string) => {
    if (value === ALL) onView({ only: undefined, focus: undefined })
    else if (value.startsWith(KIND)) {
      onView({
        only: value.slice(KIND.length) as SubjectKind,
        focus: undefined,
      })
    } else {
      // The ledger keeps its type unless that hides what's picked.
      onView({
        focus: value,
        ...(view.only && view.only !== kindOf(value) && { only: undefined }),
      })
    }
    setOpen(false)
  }

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger
        render={
          <Button
            variant="outline"
            size="sm"
            disabled={!overview}
            aria-label={`Focus: ${label}`}
            className="max-w-60 justify-between font-normal @max-[600px]/shell:max-w-36"
          />
        }
      >
        <Icon
          icon={
            focused
              ? KIND_ICONS[focused.kind]
              : view.only
                ? KIND_ICONS[view.only]
                : "dashboard"
          }
          data-icon="inline-start"
        />
        <span className="min-w-0 truncate">{label}</span>
        <Icon icon="down" data-icon="inline-end" />
      </PopoverTrigger>
      <PopoverContent align="end" className="w-80 gap-0 p-0">
        <Command filter={byName}>
          <CommandInput placeholder="Find a workflow or agent…" />
          <CommandList>
            <CommandEmpty>No workflow or agent by that name.</CommandEmpty>
            <CommandGroup>
              <CommandItem
                value={ALL}
                keywords={["All workflows and agents", "everything"]}
                data-checked={current === ALL}
                onSelect={() => pick(ALL)}
              >
                <Icon icon="dashboard" />
                All workflows and agents
              </CommandItem>
            </CommandGroup>
            {KIND_ORDER.map((kind) => {
              const items = subjects.filter((subject) => subject.kind === kind)
              if (items.length === 0) return null
              const { plural } = SUBJECT_KINDS[kind]
              return (
                <CommandGroup key={kind} heading={plural}>
                  {items.length > 1 && (
                    <CommandItem
                      value={`${KIND}${kind}`}
                      keywords={[`All ${plural.toLowerCase()}`, plural]}
                      data-checked={current === `${KIND}${kind}`}
                      onSelect={() => pick(`${KIND}${kind}`)}
                    >
                      <Icon icon={KIND_ICONS[kind]} />
                      All {plural.toLowerCase()}
                    </CommandItem>
                  )}
                  {items.map((subject) => (
                    <CommandItem
                      key={subject.key}
                      value={subject.key}
                      keywords={[subject.name]}
                      data-checked={current === subject.key}
                      onSelect={() => pick(subject.key)}
                    >
                      <Icon
                        icon={KIND_ICONS[kind]}
                        className="text-muted-foreground"
                      />
                      <span className="min-w-0 truncate">{subject.name}</span>
                      {!subject.current && kind !== "assistant" && (
                        <span className="shrink-0 text-2xs text-subtle">
                          deleted
                        </span>
                      )}
                    </CommandItem>
                  ))}
                </CommandGroup>
              )
            })}
          </CommandList>
        </Command>
      </PopoverContent>
    </Popover>
  )
}

/* Period ------------------------------------------------------------------ */

const SEGMENT =
  "inline-flex h-[26px] min-w-0 items-center gap-1.5 rounded-(--radius-soft)! px-2.5 text-xs font-normal text-muted-foreground hover:bg-transparent hover:text-foreground aria-pressed:bg-background aria-pressed:text-foreground aria-pressed:shadow-(--shadow-raised) @max-[600px]/shell:px-2"

/** A day as the API takes it, YYYY-MM-DD, from a date at the reader's midnight. */
const isoDay = (day: Date) =>
  `${day.getFullYear()}-${String(day.getMonth() + 1).padStart(2, "0")}-${String(day.getDate()).padStart(2, "0")}`

function PeriodPicker({
  view,
  onView,
}: {
  view: OverviewView
  onView: Change
}) {
  const [open, setOpen] = React.useState(false)
  const picked = view.from && view.to ? { from: view.from, to: view.to } : null
  const period = view.period ?? DEFAULT_PERIOD
  return (
    <div
      role="group"
      aria-label="Period"
      className="flex items-center rounded-(--radius-control) bg-muted p-[3px]"
    >
      <ToggleGroup
        aria-label="Last days"
        value={picked ? [] : [period]}
        onValueChange={(next) => {
          if (next[0]) onView({ period: next[0] as OverviewPeriod })
        }}
        spacing={0}
      >
        {(Object.keys(OVERVIEW_PERIODS) as OverviewPeriod[]).map((key) => (
          <ToggleGroupItem
            key={key}
            value={key}
            aria-label={`Last ${OVERVIEW_PERIODS[key].label}`}
            className={SEGMENT}
          >
            <span className="@max-[600px]/shell:hidden">
              {OVERVIEW_PERIODS[key].label}
            </span>
            <span className="hidden @max-[600px]/shell:inline">{key}</span>
          </ToggleGroupItem>
        ))}
      </ToggleGroup>
      <Popover open={open} onOpenChange={setOpen}>
        <PopoverTrigger
          render={
            <button
              type="button"
              aria-pressed={Boolean(picked)}
              aria-label={
                picked
                  ? `Days picked: ${daysLabel(picked.from, picked.to)}`
                  : "Pick days"
              }
              className={SEGMENT}
            />
          }
        >
          <Icon icon="calendar" size={14} />
          <span className="@max-[600px]/shell:hidden">
            {picked ? daysLabel(picked.from, picked.to) : "Custom"}
          </span>
        </PopoverTrigger>
        <PopoverContent align="end" className="w-auto gap-0 p-3">
          {open && (
            <DaysPicker
              picked={picked}
              onCancel={() => setOpen(false)}
              onApply={(from, to) => {
                onView({ from, to })
                setOpen(false)
              }}
            />
          )}
        </PopoverContent>
      </Popover>
    </div>
  )
}

export function DaysPicker({
  picked,
  onApply,
  onCancel,
}: {
  picked: { from: string; to: string } | null
  onApply: (from: string, to: string) => void
  onCancel: () => void
}) {
  const [today] = React.useState(() => {
    const now = new Date()
    return new Date(now.getFullYear(), now.getMonth(), now.getDate())
  })
  const [range, setRange] = React.useState<DateRange | undefined>(
    picked ? { from: dayOf(picked.from), to: dayOf(picked.to) } : undefined
  )
  // Last month and this one, so today is on the right.
  const [month, setMonth] = React.useState(
    () =>
      new Date(
        (range?.from ?? today).getFullYear(),
        (range?.from ?? today).getMonth() - (range?.from ? 0 : 1),
        1
      )
  )
  const shortcuts: [string, Date, Date][] = [
    ["This month", new Date(today.getFullYear(), today.getMonth(), 1), today],
    [
      "Last month",
      new Date(today.getFullYear(), today.getMonth() - 1, 1),
      new Date(today.getFullYear(), today.getMonth(), 0),
    ],
    ["This year", new Date(today.getFullYear(), 0, 1), today],
  ]
  const from = range?.from
  const to = range?.to ?? range?.from
  const days =
    from && to
      ? Math.round((to.getTime() - from.getTime()) / 86_400_000) + 1
      : 0

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap gap-1">
        {shortcuts.map(([label, first, last]) => (
          <Button
            key={label}
            size="xs"
            variant="outline"
            onClick={() => {
              setRange({ from: first, to: last })
              setMonth(new Date(last.getFullYear(), last.getMonth() - 1, 1))
            }}
          >
            {label}
          </Button>
        ))}
      </div>
      <Calendar
        mode="range"
        numberOfMonths={2}
        month={month}
        onMonthChange={setMonth}
        selected={range}
        onSelect={setRange}
        disabled={{ after: today }}
        endMonth={today}
        max={MAX_DAYS}
        excludeDisabled
      />
      <div className="flex items-center gap-2 border-t pt-3">
        <span className="text-xs text-muted-foreground">
          {from && to
            ? `${daysLabel(isoDay(from), isoDay(to))} · ${days.toLocaleString()} ${days === 1 ? "day" : "days"}`
            : "Pick a first day, then a last."}
        </span>
        <Button
          size="xs"
          variant="ghost"
          className="ml-auto"
          onClick={onCancel}
        >
          Cancel
        </Button>
        <Button
          size="xs"
          disabled={!from || !to}
          onClick={() => from && to && onApply(isoDay(from), isoDay(to))}
        >
          Apply
        </Button>
      </div>
    </div>
  )
}
