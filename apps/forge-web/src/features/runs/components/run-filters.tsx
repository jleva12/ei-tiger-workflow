import * as React from "react"

import { Icon } from "@/components/forge/icon"
import { Button } from "@/components/ui/button"
import {
  InputGroup,
  InputGroupAddon,
  InputGroupInput,
} from "@/components/ui/input-group"
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover"
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectLabel,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { useOrganizationChatAgents } from "@/features/agents/lib/api"
import { organizationApiKeys } from "@/features/api-keys/lib/api"
import { DaysPicker } from "@/features/overview/components/overview-controls"
import { daysLabel } from "@/features/overview/lib/overview"
import {
  shortId,
  TRIGGER_LABELS,
  type AdkRunTriggerType,
  type RunFilters,
} from "@/features/runs/lib/runs"

/*
 * Narrowing the organization's runs, or the starts it refused, for an
 * audit: to one workflow, a way they were started, a person (or key, or
 * caller), the agent they were started through, an API key, and the days
 * they started on; runs also to the tree under one run. What's picked is
 * the page's address (RunFilters), so a narrowed list is a link to share;
 * the list's summary line clears it.
 */

type Option = { value: string; label: string }

const ANY = ""

const TRIGGERS: Option[] = [
  { value: ANY, label: "Started any way" },
  ...(Object.entries(TRIGGER_LABELS) as [AdkRunTriggerType, string][]).map(
    ([value, label]) => ({ value, label })
  ),
]

/** A value settled once typing stops for a moment. */
function useDebounced<T>(value: T, ms = 400) {
  const [settled, setSettled] = React.useState(value)
  React.useEffect(() => {
    const timer = window.setTimeout(() => setSettled(value), ms)
    return () => window.clearTimeout(timer)
  }, [value, ms])
  return settled
}

/** A filter typed in (an ID), applied once typing stops. */
function TextFilter({
  value,
  onChange,
  label,
  placeholder,
  className,
}: {
  value: string | undefined
  onChange: (value: string | undefined) => void
  label: string
  placeholder: string
  className?: string
}) {
  const [typed, setTyped] = React.useState(value ?? "")
  const settled = useDebounced(typed.trim())
  // The address changing elsewhere (cleared, a link followed) is what shows.
  const [seen, setSeen] = React.useState(value)
  if (seen !== value) {
    setSeen(value)
    setTyped(value ?? "")
  }
  React.useEffect(() => {
    if ((value ?? "") !== settled && settled === typed.trim())
      onChange(settled || undefined)
    // Only what's typed settling changes the filter here.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [settled])
  return (
    <InputGroup className={className ?? "h-8 w-[min(16rem,100%)]"}>
      <InputGroupInput
        aria-label={label}
        placeholder={placeholder}
        value={typed}
        onChange={(change) => setTyped(change.target.value)}
        className="text-xs"
      />
      <InputGroupAddon align="inline-end">
        <Icon icon="search" />
      </InputGroupAddon>
    </InputGroup>
  )
}

function OptionsSelect({
  label,
  options,
  value,
  onChange,
  className,
  children,
}: {
  label: string
  options: Option[]
  value: string | undefined
  onChange: (value: string | undefined) => void
  className?: string
  /** The options, when they're grouped; a flat list of `options` otherwise. */
  children?: React.ReactNode
}) {
  return (
    <Select
      items={options}
      value={value ?? ANY}
      onValueChange={(next) => onChange(next ? String(next) : undefined)}
    >
      <SelectTrigger
        size="sm"
        aria-label={label}
        className={className ?? "max-w-56 text-xs"}
      >
        <SelectValue />
      </SelectTrigger>
      <SelectContent alignItemWithTrigger={false}>
        {children ??
          options.map((option) => (
            <SelectItem key={option.value} value={option.value}>
              {option.label}
            </SelectItem>
          ))}
      </SelectContent>
    </Select>
  )
}

/** The agent they were started through: a chat agent, or a workflow whose run started them. */
function ViaFilter({
  organizationId,
  workflows,
  value,
  onChange,
}: {
  organizationId: string
  workflows: Option[]
  value: string | undefined
  onChange: (value: string | undefined) => void
}) {
  const chat = useOrganizationChatAgents(organizationId)
  const agents = React.useMemo(
    () =>
      chat.records.map((record) => ({
        value: record.id,
        label: record.document.name || record.id,
      })),
    [chat.records]
  )
  const known = [...agents, ...workflows]
  // One the lists don't have (gone since): by its ID.
  const other =
    value && !known.some((option) => option.value === value)
      ? [{ value, label: value }]
      : []
  const options = [{ value: ANY, label: "Via any agent" }, ...known, ...other]
  return (
    <OptionsSelect
      label="Started via"
      options={options}
      value={value}
      onChange={onChange}
    >
      <SelectItem value={ANY}>Via any agent</SelectItem>
      {agents.length > 0 && (
        <SelectGroup>
          <SelectLabel>Chat agents (their Workflow tools, intakes)</SelectLabel>
          {agents.map((option) => (
            <SelectItem key={option.value} value={option.value}>
              {option.label}
            </SelectItem>
          ))}
        </SelectGroup>
      )}
      {workflows.length > 0 && (
        <SelectGroup>
          <SelectLabel>Workflows (runs their agents started)</SelectLabel>
          {workflows.map((option) => (
            <SelectItem key={option.value} value={option.value}>
              {option.label}
            </SelectItem>
          ))}
        </SelectGroup>
      )}
      {other.map((option) => (
        <SelectItem key={option.value} value={option.value}>
          {option.label}
        </SelectItem>
      ))}
    </OptionsSelect>
  )
}

/** The organization's API keys to pick from, for those who manage them. */
function KeySelect({
  organizationId,
  value,
  onChange,
}: {
  organizationId: string
  value: string | undefined
  onChange: (value: string | undefined) => void
}) {
  const keys = organizationApiKeys.scope({ organizationId }).useList()
  const listed = (keys.data ?? []).map((key) => ({
    value: key.id,
    label: key.name || key.hint,
  }))
  const options = [
    { value: ANY, label: "Any API key" },
    ...listed,
    ...(value && !listed.some((option) => option.value === value)
      ? [{ value, label: `API key ${value.slice(0, 8)}` }]
      : []),
  ]
  return (
    <OptionsSelect
      label="API key"
      options={options}
      value={value}
      onChange={onChange}
    />
  )
}

/** The days they started on, picked on a calendar. */
function DaysFilter({
  from,
  to,
  onChange,
}: {
  from: string | undefined
  to: string | undefined
  onChange: (from: string | undefined, to: string | undefined) => void
}) {
  const [open, setOpen] = React.useState(false)
  const label =
    from || to
      ? from && to
        ? daysLabel(from, to)
        : from
          ? `Since ${daysLabel(from, from)}`
          : `Until ${daysLabel(to!, to!)}`
      : "Any time"
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger
        render={
          <Button
            variant="outline"
            size="sm"
            aria-label={`Started: ${label}`}
            className="text-xs font-normal"
          />
        }
      >
        <Icon icon="calendar" data-icon="inline-start" />
        {label}
      </PopoverTrigger>
      <PopoverContent align="start" className="w-auto gap-0 p-3">
        {open && (
          <div className="flex flex-col gap-3">
            {(from || to) && (
              <Button
                size="xs"
                variant="outline"
                className="self-start"
                onClick={() => {
                  onChange(undefined, undefined)
                  setOpen(false)
                }}
              >
                Any time
              </Button>
            )}
            <DaysPicker
              picked={from && to ? { from, to } : null}
              onCancel={() => setOpen(false)}
              onApply={(first, last) => {
                onChange(first, last)
                setOpen(false)
              }}
            />
          </div>
        )}
      </PopoverContent>
    </Popover>
  )
}

/**
 * The filters over a list of runs or refused starts.
 *
 * :param workflows: The organization's workflows, by ID and name.
 * :param canListKeys: Whether the caller may list the organization's API
 *   keys (`api_keys:manage`); otherwise a key is typed in by its ID.
 * :param tree: Whether the list is of runs, which may be narrowed to the
 *   tree under one (`root`).
 */
export function RunFiltersBar({
  organizationId,
  filters,
  onChange,
  workflows,
  canListKeys,
  tree,
}: {
  organizationId: string
  filters: RunFilters
  onChange: (filters: RunFilters) => void
  workflows: Option[]
  canListKeys: boolean
  tree: boolean
}) {
  const set = (change: Partial<RunFilters>) =>
    onChange({ ...filters, ...change })
  const workflowOptions = [
    { value: ANY, label: "All workflows" },
    ...workflows,
    ...(filters.workflow &&
    !workflows.some((option) => option.value === filters.workflow)
      ? [{ value: filters.workflow, label: filters.workflow }]
      : []),
  ]
  return (
    <div
      role="group"
      aria-label="Filters"
      className="flex flex-wrap items-center gap-2"
    >
      <OptionsSelect
        label="Workflow"
        options={workflowOptions}
        value={filters.workflow}
        onChange={(workflow) => set({ workflow })}
      />
      <OptionsSelect
        label="How they were started"
        options={TRIGGERS}
        value={filters.trigger}
        onChange={(trigger) =>
          set({ trigger: trigger as AdkRunTriggerType | undefined })
        }
      />
      <TextFilter
        label="For whom: a person's, API key's or caller's ID"
        placeholder="For whom: user or key ID…"
        value={filters.person}
        onChange={(person) => set({ person })}
      />
      <ViaFilter
        organizationId={organizationId}
        workflows={workflows}
        value={filters.via}
        onChange={(via) => set({ via })}
      />
      {canListKeys ? (
        <KeySelect
          organizationId={organizationId}
          value={filters.apiKey}
          onChange={(apiKey) => set({ apiKey })}
        />
      ) : (
        <TextFilter
          label="API key ID"
          placeholder="API key ID…"
          className="h-8 w-[min(12rem,100%)]"
          value={filters.apiKey}
          onChange={(apiKey) => set({ apiKey })}
        />
      )}
      <DaysFilter
        from={filters.startedFrom}
        to={filters.startedTo}
        onChange={(startedFrom, startedTo) => set({ startedFrom, startedTo })}
      />
      {tree && filters.root && (
        <Button
          variant="secondary"
          size="sm"
          className="text-xs"
          aria-label={`Only the tree under run ${shortId(filters.root)}: show every run`}
          onClick={() => set({ root: undefined })}
        >
          Under run <code>{shortId(filters.root)}</code>
          <Icon icon="close" data-icon="inline-end" />
        </Button>
      )}
    </div>
  )
}
