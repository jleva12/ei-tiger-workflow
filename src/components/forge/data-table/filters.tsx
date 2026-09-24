import * as React from "react"
import { cn } from "cn"
import type { RowData } from "@tanstack/react-table"

import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Input } from "@/components/ui/input"
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
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Icon } from "../icon"
import { ToolbarButton } from "../toolbar"
import { useDataTable } from "./context"
import type { Column } from "./table-config"
import { formatValue, getColumnLabel } from "./utils"

type FilterSize = "sm" | "default"

type FilterOption = {
  key: string
  label: string
  value: unknown
  count?: number
}

const ALL = "__all"

const controlSize: Record<FilterSize, string> = {
  sm: "h-7 px-2 text-2xs font-normal md:text-2xs",
  default: "h-8 px-2.5 text-xs md:text-xs",
}

/** Options for select filters: fixed `meta.filterOptions`, else faceted values. */
function useFilterOptions(
  column: Column<RowData, unknown>,
  enabled: boolean
): FilterOption[] {
  const { features } = useDataTable()
  const meta = column.columnDef.meta
  // Faceting walks every row, so only option-based filters pay for it.
  const facets =
    enabled && features.faceting ? column.getFacetedUniqueValues() : undefined
  return React.useMemo(() => {
    if (meta?.filterOptions) {
      return meta.filterOptions.map((option, index) => ({
        key: String(index),
        label: option.label,
        value: option.value,
        count: facets?.get(option.value),
      }))
    }
    if (!facets) return []
    return [...facets.entries()]
      .filter(
        ([value]) => value !== null && value !== undefined && value !== ""
      )
      .sort(([a], [b]) =>
        typeof a === "number" && typeof b === "number"
          ? a - b
          : String(a).localeCompare(String(b), undefined, { numeric: true })
      )
      .slice(0, 500)
      .map(([value, count], index) => ({
        key: String(index),
        label: formatValue(value, meta) || String(value),
        value,
        count,
      }))
  }, [facets, meta])
}

function TextFilter({
  column,
  size,
}: {
  column: Column<RowData, unknown>
  size: FilterSize
}) {
  const { labels } = useDataTable()
  const meta = column.columnDef.meta
  return (
    <Input
      value={(column.getFilterValue() as string) ?? ""}
      onChange={(event) => column.setFilterValue(event.target.value)}
      placeholder={
        meta?.filterPlaceholder ?? `${labels.filter} ${getColumnLabel(column)}`
      }
      aria-label={`${labels.filter} ${getColumnLabel(column)}`}
      className={controlSize[size]}
    />
  )
}

/**
 * Two bounds; either may be empty. Used for number and date ranges. In the
 * header row the inputs sit in a popover behind a one-line summary.
 */
function RangeFilter({
  column,
  size,
  type,
}: {
  column: Column<RowData, unknown>
  size: FilterSize
  type: "number" | "date"
}) {
  const { features, labels } = useDataTable()
  const value =
    (column.getFilterValue() as [unknown, unknown] | undefined) ?? []
  const meta = column.columnDef.meta
  const bounds =
    type === "number" && features.faceting
      ? column.getFacetedMinMaxValues()
      : undefined
  const label = getColumnLabel(column)
  const update = (index: 0 | 1, next: string) => {
    // Date bounds cover whole local days, so "to" includes that day.
    const parsed =
      next === ""
        ? undefined
        : type === "number"
          ? Number(next)
          : `${next}T${index === 0 ? "00:00:00" : "23:59:59.999"}`
    const range: [unknown, unknown] = [value[0], value[1]]
    range[index] = parsed
    column.setFilterValue(
      range[0] === undefined && range[1] === undefined ? undefined : range
    )
  }
  const show = (bound: unknown) =>
    type === "date"
      ? formatValue(String(bound).slice(0, 10), { format: "date" })
      : typeof meta?.format === "string"
        ? formatValue(bound, meta)
        : String(bound)

  const inputs = (inputSize: FilterSize) => (
    <div className="flex items-center gap-1">
      {([0, 1] as const).map((index) => (
        <Input
          key={index}
          type={type}
          inputMode={type === "number" ? "decimal" : undefined}
          value={
            type === "date"
              ? String(value[index] ?? "").slice(0, 10)
              : ((value[index] as number | undefined) ?? "")
          }
          onChange={(event) => update(index, event.target.value)}
          placeholder={
            bounds
              ? `${index === 0 ? labels.min : labels.max} ${formatValue(bounds[index], meta)}`
              : index === 0
                ? type === "date"
                  ? labels.from
                  : labels.min
                : type === "date"
                  ? labels.to
                  : labels.max
          }
          aria-label={`${label} ${index === 0 ? labels.min : labels.max}`}
          className={cn(controlSize[inputSize], "min-w-0 tabular-nums")}
        />
      ))}
    </div>
  )

  if (size !== "sm") return inputs(size)

  const [from, to] = value
  const active = from !== undefined || to !== undefined
  const summary =
    from !== undefined && to !== undefined
      ? `${show(from)} – ${show(to)}`
      : from !== undefined
        ? `≥ ${show(from)}`
        : to !== undefined
          ? `≤ ${show(to)}`
          : labels.all
  return (
    <Popover>
      <PopoverTrigger
        render={
          <Button
            variant="outline"
            aria-label={`${labels.filter} ${label}`}
            className={cn(
              controlSize.sm,
              "w-full justify-between gap-1 bg-background font-normal",
              active && "text-foreground"
            )}
          />
        }
      >
        <span className="truncate tabular-nums">{summary}</span>
        <Icon icon="down" size={12} className="shrink-0 text-subtle" />
      </PopoverTrigger>
      <PopoverContent align="start" className="w-64 gap-2.5 p-3">
        <span className="text-2xs text-muted-foreground">{label}</span>
        {inputs("default")}
        {active ? (
          <Button
            variant="ghost"
            size="xs"
            className="self-start text-muted-foreground"
            onClick={() => column.setFilterValue(undefined)}
          >
            {labels.clearFilters}
          </Button>
        ) : null}
      </PopoverContent>
    </Popover>
  )
}

function SelectFilter({
  column,
  size,
  options,
}: {
  column: Column<RowData, unknown>
  size: FilterSize
  options: FilterOption[]
}) {
  const { labels } = useDataTable()
  const current = column.getFilterValue()
  const selected = options.find((option) => Object.is(option.value, current))
  const items = [
    { value: ALL, label: labels.all },
    ...options.map((option) => ({ value: option.key, label: option.label })),
  ]
  return (
    <Select
      value={selected?.key ?? ALL}
      items={items}
      onValueChange={(key) => {
        const option = options.find((item) => item.key === key)
        column.setFilterValue(option ? option.value : undefined)
      }}
    >
      <SelectTrigger
        size="sm"
        aria-label={`${labels.filter} ${getColumnLabel(column)}`}
        className={cn(controlSize[size], "w-full")}
      >
        <SelectValue />
      </SelectTrigger>
      <SelectContent alignItemWithTrigger={false} className="max-h-72">
        <SelectGroup>
          <SelectItem value={ALL}>{labels.all}</SelectItem>
          {options.map((option) => (
            <SelectItem key={option.key} value={option.key}>
              <span className="truncate">{option.label}</span>
              {option.count !== undefined && (
                <span className="ml-auto text-3xs text-subtle tabular-nums">
                  {option.count}
                </span>
              )}
            </SelectItem>
          ))}
        </SelectGroup>
      </SelectContent>
    </Select>
  )
}

function MultiSelectFilter({
  column,
  size,
  options,
}: {
  column: Column<RowData, unknown>
  size: FilterSize
  options: FilterOption[]
}) {
  const { labels } = useDataTable()
  const [query, setQuery] = React.useState("")
  const selected = (column.getFilterValue() as unknown[] | undefined) ?? []
  const shown = options.filter((option) =>
    option.label.toLowerCase().includes(query.trim().toLowerCase())
  )
  const toggle = (value: unknown, checked: boolean) => {
    const next = checked
      ? [...selected, value]
      : selected.filter((item) => !Object.is(item, value))
    column.setFilterValue(next.length ? next : undefined)
  }
  const summary =
    selected.length === 0
      ? labels.all
      : selected.length === 1
        ? (options.find((option) => Object.is(option.value, selected[0]))
            ?.label ?? String(selected[0]))
        : `${selected.length} ${labels.selectedValues}`
  return (
    <Popover>
      <PopoverTrigger
        render={
          <Button
            variant="outline"
            aria-label={`${labels.filter} ${getColumnLabel(column)}`}
            className={cn(
              controlSize[size],
              "w-full justify-between gap-1 bg-background font-normal",
              selected.length > 0 && "text-foreground"
            )}
          />
        }
      >
        <span className="truncate">{summary}</span>
        <Icon icon="down" size={12} className="shrink-0 text-subtle" />
      </PopoverTrigger>
      <PopoverContent align="start" className="w-60 gap-2 p-2">
        <InputGroup className="h-7">
          <InputGroupAddon>
            <Icon icon="search" size={14} />
          </InputGroupAddon>
          <InputGroupInput
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder={labels.searchValues}
            aria-label={labels.searchValues}
            className="text-2xs md:text-2xs"
          />
        </InputGroup>
        <div
          role="group"
          aria-label={getColumnLabel(column)}
          className="-mx-1 max-h-64 overflow-y-auto"
        >
          {shown.map((option) => {
            const checked = selected.some((item) =>
              Object.is(item, option.value)
            )
            return (
              <label
                key={option.key}
                className="flex min-h-8 cursor-default items-center gap-2 rounded-(--radius-item) px-2 text-xs hover:bg-accent"
              >
                <Checkbox
                  checked={checked}
                  onCheckedChange={(next) => toggle(option.value, next)}
                />
                <span className="min-w-0 flex-1 truncate">{option.label}</span>
                {option.count !== undefined && (
                  <span className="text-3xs text-subtle tabular-nums">
                    {option.count}
                  </span>
                )}
              </label>
            )
          })}
          {!shown.length && (
            <p className="px-2 py-3 text-2xs text-subtle">{labels.noValues}</p>
          )}
        </div>
        {selected.length > 0 && (
          <Button
            variant="ghost"
            size="xs"
            className="self-start text-muted-foreground"
            onClick={() => column.setFilterValue(undefined)}
          >
            {labels.clearFilters}
          </Button>
        )}
      </PopoverContent>
    </Popover>
  )
}

function BooleanFilter({
  column,
  size,
}: {
  column: Column<RowData, unknown>
  size: FilterSize
}) {
  const { labels } = useDataTable()
  const value = column.getFilterValue()
  const items = [
    { value: ALL, label: labels.all },
    { value: "true", label: labels.yes },
    { value: "false", label: labels.no },
  ]
  return (
    <Select
      value={value === true ? "true" : value === false ? "false" : ALL}
      items={items}
      onValueChange={(next) =>
        column.setFilterValue(
          next === "true" ? true : next === "false" ? false : undefined
        )
      }
    >
      <SelectTrigger
        size="sm"
        aria-label={`${labels.filter} ${getColumnLabel(column)}`}
        className={cn(controlSize[size], "w-full")}
      >
        <SelectValue />
      </SelectTrigger>
      <SelectContent alignItemWithTrigger={false}>
        <SelectGroup>
          {items.map((item) => (
            <SelectItem key={item.value} value={item.value}>
              {item.label}
            </SelectItem>
          ))}
        </SelectGroup>
      </SelectContent>
    </Select>
  )
}

/** The filter control for a column, chosen by `meta.filterVariant`. */
function ColumnFilter({
  column,
  size = "sm",
}: {
  column: Column<RowData, unknown>
  size?: FilterSize
}) {
  const variant = column.columnDef.meta?.filterVariant ?? "text"
  const options = useFilterOptions(
    column,
    variant === "select" || variant === "multiSelect"
  )
  if (!column.getCanFilter() || variant === "none") return null
  switch (variant) {
    case "range":
      return <RangeFilter column={column} size={size} type="number" />
    case "date":
      return <RangeFilter column={column} size={size} type="date" />
    case "select":
      return <SelectFilter column={column} size={size} options={options} />
    case "multiSelect":
      return <MultiSelectFilter column={column} size={size} options={options} />
    case "boolean":
      return <BooleanFilter column={column} size={size} />
    default:
      return <TextFilter column={column} size={size} />
  }
}

/** Toolbar popover listing every filterable column's control. */
function FilterPanel() {
  const { table, labels } = useDataTable()
  const columns = table
    .getVisibleLeafColumns()
    .filter(
      (column) =>
        column.getCanFilter() && column.columnDef.meta?.filterVariant !== "none"
    )
  const active = table.atoms.columnFilters.get().length
  return (
    <Popover>
      <PopoverTrigger
        render={<ToolbarButton icon="filter" active={active > 0} />}
      >
        {labels.filters}
      </PopoverTrigger>
      <PopoverContent align="end" className="w-80 gap-3.5">
        <div className="flex items-center justify-between">
          <span className="text-xs font-medium">{labels.filters}</span>
          {active > 0 && (
            <Button
              variant="ghost"
              size="xs"
              className="text-muted-foreground"
              onClick={() => table.resetColumnFilters()}
            >
              {labels.clearFilters}
            </Button>
          )}
        </div>
        <div className="-mx-3 flex max-h-[60vh] flex-col gap-3 overflow-y-auto px-3">
          {columns.map((column) => (
            <div key={column.id} className="flex flex-col gap-1.5">
              <span
                className={cn(
                  "text-2xs text-muted-foreground",
                  column.getIsFiltered() && "text-foreground"
                )}
              >
                {getColumnLabel(column)}
              </span>
              <ColumnFilter column={column} size="default" />
            </div>
          ))}
        </div>
      </PopoverContent>
    </Popover>
  )
}

export { ColumnFilter, FilterPanel }
