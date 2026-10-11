import * as React from "react"
import { cn } from "cn"

import { Icon } from "@/components/forge/icon"

// Strings longer than this show their start, with a way to show the rest.
const LONG = 280

type Props = {
  value: unknown
  /** How many levels open at first; deeper ones open on a click. */
  openDepth?: number
  /** Keys painted as changed, at the top level. */
  highlight?: ReadonlySet<string>
  /** Keys painted as Forge's own (quieter), at the top level. */
  quiet?: (key: string) => boolean
  className?: string
}

/**
 * JSON as a tree to explore: objects and arrays open and close, each with
 * how many keys or items it holds, long strings cut short until asked.
 */
export function ValueTree({
  value,
  openDepth = 1,
  highlight,
  quiet,
  className,
}: Props) {
  return (
    <div
      tabIndex={0}
      className={cn(
        "overflow-auto rounded-(--radius-control) border bg-muted p-3 font-mono text-2xs/[1.7] text-muted-foreground",
        className
      )}
    >
      {isBranch(value) ? (
        <Entries
          value={value}
          depth={0}
          openDepth={openDepth}
          highlight={highlight}
          quiet={quiet}
        />
      ) : (
        <Leaf value={value} />
      )}
    </div>
  )
}

type Branch = Record<string, unknown> | unknown[]

const isBranch = (value: unknown): value is Branch =>
  typeof value === "object" && value !== null

const sizeOf = (value: Branch) =>
  Array.isArray(value)
    ? `[${value.length} ${value.length === 1 ? "item" : "items"}]`
    : `{${Object.keys(value).length} ${Object.keys(value).length === 1 ? "key" : "keys"}}`

function Entries({
  value,
  depth,
  openDepth,
  highlight,
  quiet,
}: {
  value: Branch
  depth: number
  openDepth: number
  highlight?: ReadonlySet<string>
  quiet?: (key: string) => boolean
}) {
  const entries = Array.isArray(value)
    ? value.map((item, index) => [String(index), item] as const)
    : Object.entries(value)
  if (!entries.length) return <span>{Array.isArray(value) ? "[]" : "{}"}</span>
  return (
    <ul className={cn("flex flex-col", depth > 0 && "border-l pl-3")}>
      {entries.map(([key, item]) => (
        <Entry
          key={key}
          name={key}
          index={Array.isArray(value)}
          value={item}
          depth={depth}
          openDepth={openDepth}
          changed={depth === 0 && highlight?.has(key)}
          muted={depth === 0 && quiet?.(key)}
        />
      ))}
    </ul>
  )
}

function Entry({
  name,
  index,
  value,
  depth,
  openDepth,
  changed,
  muted,
}: {
  name: string
  index: boolean
  value: unknown
  depth: number
  openDepth: number
  changed?: boolean
  muted?: boolean
}) {
  const branch = isBranch(value)
  const [open, setOpen] = React.useState(depth < openDepth)
  const label = (
    <span
      className={cn(
        index ? "text-subtle" : "text-foreground",
        muted && "text-subtle"
      )}
    >
      {index ? name : `"${name}"`}
    </span>
  )
  return (
    <li
      className={cn(
        "min-w-0",
        changed && "-mx-1 rounded-(--radius-soft) bg-warning-surface px-1"
      )}
    >
      {branch ? (
        <>
          <button
            type="button"
            aria-expanded={open}
            onClick={() => setOpen((was) => !was)}
            className="inline-flex max-w-full items-center gap-1 rounded-(--radius-soft) text-left hover:text-foreground"
          >
            <Icon
              icon="right"
              size={10}
              className={cn(
                "shrink-0 transition-transform duration-150",
                open && "rotate-90"
              )}
            />
            {label}
            <span className="text-subtle">: {sizeOf(value)}</span>
          </button>
          {open && (
            <div className="ml-[5px]">
              <Entries value={value} depth={depth + 1} openDepth={openDepth} />
            </div>
          )}
        </>
      ) : (
        <span className="flex min-w-0 gap-1 pl-[14px]">
          <span className="shrink-0">{label}:</span>
          <Leaf value={value} />
        </span>
      )}
    </li>
  )
}

function Leaf({ value }: { value: unknown }) {
  const [whole, setWhole] = React.useState(false)
  if (typeof value === "string") {
    const long = value.length > LONG
    const shown = long && !whole ? `${value.slice(0, LONG)}…` : value
    return (
      <span className="min-w-0 wrap-anywhere whitespace-pre-wrap text-tone-green-foreground">
        &quot;{shown}&quot;
        {long && (
          <button
            type="button"
            onClick={() => setWhole((was) => !was)}
            className="ml-1.5 font-sans text-3xs text-muted-foreground underline underline-offset-2 hover:text-foreground"
          >
            {whole
              ? "Show less"
              : `Show all ${value.length.toLocaleString()} characters`}
          </button>
        )}
      </span>
    )
  }
  if (typeof value === "number")
    return <span className="text-tone-amber-foreground">{value}</span>
  if (typeof value === "boolean" || value === null)
    return <span className="text-tone-pink-foreground">{String(value)}</span>
  if (value === undefined) return <span className="text-subtle">unset</span>
  return <span>{JSON.stringify(value)}</span>
}
