import * as React from "react"
import { cn } from "cn"

import { Icon } from "@/components/forge/icon"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { parseTimestamp } from "@/lib/timestamps"
import { useActorLabel } from "@/lib/users"
import { FACTS, type Part } from "../lib/map-parts"
import { COMPONENT_KINDS, type Group } from "../lib/system"

/*
 * Pieces the system map's inspector and dialogs share: how a row shows an
 * application or a component, a group picker, who added something.
 */

// The applications' markers, by their place in the knowledge base's list.
const COLORS = [
  "bg-project-1",
  "bg-project-2",
  "bg-project-3",
  "bg-project-4",
] as const

// What a group picker shows for none.
const NO_GROUP = "none"

const addedFormat = new Intl.DateTimeFormat(undefined, {
  dateStyle: "medium",
  timeStyle: "short",
})

/** An application's colour, as its marker in the list. */
export function Marker({ color }: { color: number }) {
  return (
    <span
      aria-hidden="true"
      className={cn(
        "size-2 shrink-0 rounded-[3px]",
        COLORS[color % COLORS.length]
      )}
    />
  )
}

/** What leads something's row: an application's colour, a component's kind. */
export function PartLead({ part }: { part: Part }) {
  return part.type === "application" ? (
    <Marker color={part.color} />
  ) : (
    <Icon
      icon={COMPONENT_KINDS[part.component.kind].icon}
      size={14}
      aria-hidden="true"
      className="shrink-0 text-muted-foreground"
    />
  )
}

export function RowButton({
  onClick,
  lead,
  title,
  detail,
}: {
  onClick: () => void
  lead: React.ReactNode
  title: string
  detail: string
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="flex w-full min-w-0 flex-1 items-center gap-2 rounded-(--radius-item) px-1.5 py-1 text-left text-xs hover:bg-muted"
    >
      {lead}
      <span className="flex min-w-0 flex-col">
        <span className="truncate text-foreground">{title}</span>
        <span className="truncate text-2xs text-subtle">{detail}</span>
      </span>
    </button>
  )
}

export function SectionHeading({ children }: { children: React.ReactNode }) {
  return (
    <h5 className="text-2xs font-medium text-muted-foreground">{children}</h5>
  )
}

/** A select of the knowledge base's groups, with none. */
export function GroupPicker({
  id,
  value,
  groups,
  onChange,
}: {
  id: string
  value: string | undefined
  groups: Group[]
  onChange: (groupId: string | null) => void
}) {
  const items = [
    { value: NO_GROUP, label: "No group" },
    ...groups.map((group) => ({ value: group.id, label: group.name })),
  ]
  return (
    <Select
      items={items}
      value={value ?? NO_GROUP}
      onValueChange={(next) =>
        onChange(next && next !== NO_GROUP ? String(next) : null)
      }
    >
      <SelectTrigger id={id} size="sm" className="w-full">
        <SelectValue />
      </SelectTrigger>
      <SelectContent alignItemWithTrigger={false}>
        {items.map((item) => (
          <SelectItem key={item.value} value={item.value}>
            {item.label}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  )
}

/** Who added something, and when. */
export function AddedFacts({
  record,
  children,
}: {
  record: { created_by: string; created_at: string }
  children?: React.ReactNode
}) {
  const actor = useActorLabel()
  return (
    <dl className={FACTS}>
      {children}
      <dt className="leading-5 text-muted-foreground">Added by</dt>
      <dd className="min-w-0 leading-5 break-words text-foreground">
        {actor(record.created_by)}
      </dd>
      <dt className="leading-5 text-muted-foreground">Added</dt>
      <dd className="min-w-0 leading-5 text-foreground">
        {addedFormat.format(parseTimestamp(record.created_at))}
      </dd>
    </dl>
  )
}
