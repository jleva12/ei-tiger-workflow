import * as React from "react"

import { Icon } from "@/components/forge/icon"
import { Button } from "@/components/ui/button"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import type { CodeRepository } from "@/features/code-repositories/lib/api"
import { fullName } from "@/features/code-repositories/lib/display"
import type { KnowledgeScope } from "../lib/api"
import {
  COMPONENT_KIND_ITEMS,
  COMPONENT_KINDS,
  CONNECTION_KINDS,
  GROUP_ICON,
  endOf,
  touches,
  type Component,
  type ComponentKind,
  type Connection,
  type Group,
  type MapEnd,
} from "../lib/system"
import { CodeLinksSection } from "./code-links"
import type { MapSelection } from "./system-map-canvas"
import {
  componentKind,
  endFrom,
  endValue,
  FACTS,
  plural,
  type Part,
  type Parts,
  type Removing,
} from "../lib/map-parts"
import {
  AddedFacts,
  GroupPicker,
  Marker,
  PartLead,
  RowButton,
  SectionHeading,
} from "./system-map-parts"

/*
 * The system map's inspector: the whole system when nothing's selected, or
 * the application, component, group or connection that is.
 */

/**
 * Nothing selected: the whole system, its groups (with what's in each),
 * its applications and its components, each with how connected it is.
 */
export function SystemOverview({
  baseName,
  applications,
  components,
  groups,
  membership,
  connections,
  canManage,
  onSelect,
}: {
  baseName: string
  applications: CodeRepository[]
  components: Component[]
  groups: Group[]
  membership: Record<string, string>
  connections: Connection[]
  canManage: boolean
  onSelect: (selection: MapSelection) => void
}) {
  const pending = applications.some((repo) => !repo.last_success)
  const connected = (id: string) => {
    const count = connections.filter((link) => touches(link, id)).length
    return count ? plural(count, "connection") : "Not connected"
  }
  const inside = (groupId: string) =>
    Object.values(membership).filter((group) => group === groupId).length
  return (
    <div className="flex min-w-0 flex-col gap-4 text-xs">
      <p className="text-muted-foreground">
        {canManage
          ? `Select something to see how it connects, or to connect it. Add puts what ${baseName}'s code doesn't show on the map — databases, queues, external systems — and groups that say what each part of the system is for. Agents are told all of it.`
          : "Select something to see how it connects to the rest of the system."}
      </p>
      {groups.length > 0 && (
        <section className="flex flex-col gap-1.5">
          <SectionHeading>Groups</SectionHeading>
          <ul className="-mx-1 flex flex-col" aria-label="Groups">
            {groups.map((group) => (
              <li key={group.id}>
                <RowButton
                  onClick={() => onSelect({ type: "group", id: group.id })}
                  lead={
                    <Icon
                      icon={GROUP_ICON}
                      size={14}
                      aria-hidden="true"
                      className="shrink-0 text-muted-foreground"
                    />
                  }
                  title={group.name}
                  detail={
                    inside(group.id)
                      ? `${inside(group.id)} inside${group.description ? ` · ${group.description}` : ""}`
                      : group.description || "Empty"
                  }
                />
              </li>
            ))}
          </ul>
        </section>
      )}
      <section className="flex flex-col gap-1.5">
        <SectionHeading>Applications</SectionHeading>
        {applications.length ? (
          <ul className="-mx-1 flex flex-col" aria-label="Applications">
            {applications.map((repo, index) => (
              <li key={repo.id}>
                <RowButton
                  onClick={() => onSelect({ type: "application", id: repo.id })}
                  lead={<Marker color={index} />}
                  title={repo.name}
                  detail={connected(repo.id)}
                />
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-subtle">
            No applications yet: their code is what agents search.
          </p>
        )}
      </section>
      {components.length > 0 && (
        <section className="flex flex-col gap-1.5">
          <SectionHeading>Components</SectionHeading>
          <ul className="-mx-1 flex flex-col" aria-label="Components">
            {components.map((component) => (
              <li key={component.id}>
                <RowButton
                  onClick={() =>
                    onSelect({ type: "component", id: component.id })
                  }
                  lead={
                    <Icon
                      icon={COMPONENT_KINDS[component.kind].icon}
                      size={14}
                      aria-hidden="true"
                      className="shrink-0 text-muted-foreground"
                    />
                  }
                  title={component.name}
                  detail={`${componentKind(component)} · ${connected(component.id)}`}
                />
              </li>
            ))}
          </ul>
        </section>
      )}
      {pending && (
        <p className="flex items-center gap-1.5 text-2xs text-subtle">
          <span
            aria-hidden="true"
            className="h-2.5 w-4 shrink-0 rounded-[3px] border border-dashed border-muted-foreground"
          />
          Dashed: not in the code graph yet
        </p>
      )}
    </div>
  )
}

/** Something's connections each way, each opening the connection. */
function ConnectionLists({
  id,
  connections,
  parts,
  canManage,
  onSelect,
  onRemove,
}: {
  id: string
  connections: Connection[]
  parts: Parts
  canManage: boolean
  onSelect: (selection: MapSelection) => void
  onRemove: (removing: Removing) => void
}) {
  const outgoing = connections.filter((link) => endOf(link, "source").id === id)
  const incoming = connections.filter((link) => endOf(link, "target").id === id)
  const section = (
    title: string,
    list: Connection[],
    other: "source" | "target"
  ) =>
    list.length > 0 && (
      <section className="flex flex-col gap-1.5">
        <SectionHeading>{title}</SectionHeading>
        <ul className="-mx-1 flex flex-col">
          {list.map((link) => {
            const end = parts.get(endOf(link, other).id)
            if (!end) return null
            const kind = [
              CONNECTION_KINDS[link.kind]?.label ?? link.kind,
              link.code_links > 0 && plural(link.code_links, "code link"),
            ]
              .filter(Boolean)
              .join(" · ")
            return (
              <li key={link.id} className="flex items-center gap-1">
                <RowButton
                  onClick={() => onSelect({ type: "connection", id: link.id })}
                  lead={<PartLead part={end} />}
                  title={end.name}
                  detail={
                    link.description ? `${kind} · ${link.description}` : kind
                  }
                />
                {canManage && (
                  <Button
                    variant="ghost"
                    size="icon-xs"
                    aria-label={`Remove the connection ${other === "target" ? "to" : "from"} ${end.name}`}
                    onClick={() =>
                      onRemove({ type: "connection", connection: link })
                    }
                  >
                    <Icon icon="close" />
                  </Button>
                )}
              </li>
            )
          })}
        </ul>
      </section>
    )
  return (
    <>
      {section("Connects to", outgoing, "target")}
      {section("Connected from", incoming, "source")}
    </>
  )
}

/**
 * A selected application or component: what it is, which group it's in,
 * and its connections each way.
 */
export function PartDetails({
  baseName,
  part,
  connections,
  parts,
  groups,
  group,
  canManage,
  alone,
  onConnect,
  onGroup,
  onOpen,
  onEdit,
  onSelect,
  onRemove,
}: {
  baseName: string
  part: Part
  connections: Connection[]
  parts: Parts
  groups: Group[]
  /** The group it's in. */
  group: string | undefined
  canManage: boolean
  /** It's the only thing on the map: nothing to connect it to. */
  alone: boolean
  onConnect: () => void
  onGroup: (end: MapEnd, groupId: string | null) => void
  onOpen: (id: string) => void
  onEdit: (component: Component) => void
  onSelect: (selection: MapSelection) => void
  onRemove: (removing: Removing) => void
}) {
  const id = React.useId()
  const connected = connections.some((link) => touches(link, part.id))
  const groupName = groups.find((g) => g.id === group)?.name
  return (
    <div className="flex min-w-0 flex-col gap-4 text-xs">
      <div className="flex flex-col gap-1">
        <span className="flex items-center gap-1.5 text-2xs text-muted-foreground">
          <PartLead part={part} />
          {part.type === "application"
            ? `Application${part.repo.last_success ? "" : " · not in the code graph yet"}`
            : componentKind(part.component)}
        </span>
        <h4 className="text-sm font-medium break-words text-foreground">
          {part.name}
        </h4>
        {part.type === "application" ? (
          <a
            href={part.repo.url}
            target="_blank"
            rel="noreferrer"
            className="flex w-fit max-w-full min-w-0 items-center gap-1 font-mono text-2xs text-muted-foreground hover:text-foreground hover:underline"
          >
            <span className="truncate">
              {fullName(part.repo)} · {part.repo.branch}
            </span>
            <Icon icon="external" size={12} className="shrink-0" />
          </a>
        ) : part.component.description ? (
          <p className="mt-1 break-words whitespace-pre-wrap text-foreground">
            {part.component.description}
          </p>
        ) : (
          <p className="text-subtle">
            {canManage
              ? "No description yet: say what it holds or does, for agents."
              : "No description."}
          </p>
        )}
      </div>
      <div className="flex flex-wrap gap-1.5 *:flex-1">
        {canManage && (
          <Button size="sm" disabled={alone} onClick={onConnect}>
            <Icon icon="link" data-icon="inline-start" />
            Connect to another
          </Button>
        )}
        {part.type === "application" ? (
          <Button size="sm" variant="outline" onClick={() => onOpen(part.id)}>
            <Icon icon="layers" data-icon="inline-start" />
            {part.repo.last_success ? "Open code graph" : "Open application"}
          </Button>
        ) : (
          canManage && (
            <Button
              size="sm"
              variant="outline"
              onClick={() => onEdit(part.component)}
            >
              Edit
            </Button>
          )
        )}
      </div>
      {(canManage ? groups.length > 0 : groupName) && (
        <dl className={FACTS}>
          <dt className="leading-7 text-muted-foreground">
            <label htmlFor={`${id}-group`}>Group</label>
          </dt>
          <dd className="min-w-0 leading-7 text-foreground">
            {canManage ? (
              <GroupPicker
                id={`${id}-group`}
                value={group}
                groups={groups}
                onChange={(next) =>
                  onGroup({ type: part.type, id: part.id }, next)
                }
              />
            ) : (
              groupName
            )}
          </dd>
        </dl>
      )}
      <ConnectionLists
        id={part.id}
        connections={connections}
        parts={parts}
        canManage={canManage}
        onSelect={onSelect}
        onRemove={onRemove}
      />
      {!connected && (
        <p className="text-subtle">
          {alone
            ? `It's the only thing on ${baseName}'s map.`
            : canManage
              ? "Not connected to anything yet."
              : "Not connected to anything yet. Those who manage knowledge bases can connect it."}
        </p>
      )}
      {part.type === "component" && (
        <>
          <AddedFacts record={part.component} />
          {canManage && (
            <Button
              size="sm"
              variant="destructive"
              className="self-start"
              onClick={() =>
                onRemove({ type: "component", component: part.component })
              }
            >
              Remove component
            </Button>
          )}
        </>
      )}
    </div>
  )
}

/**
 * A selected group: what it is and what it's for, what's in it, and ways
 * to put more in.
 */
export function GroupDetails({
  group,
  parts,
  membership,
  canManage,
  onGroup,
  onAddComponent,
  onEdit,
  onSelect,
  onRemove,
}: {
  group: Group
  parts: Parts
  membership: Record<string, string>
  canManage: boolean
  onGroup: (end: MapEnd, groupId: string | null) => void
  onAddComponent: (kind: ComponentKind) => void
  onEdit: () => void
  onSelect: (selection: MapSelection) => void
  onRemove: () => void
}) {
  const id = React.useId()
  const members = [...parts.values()].filter(
    (part) => membership[part.id] === group.id
  )
  const others = [...parts.values()].filter(
    (part) => membership[part.id] !== group.id
  )
  const items = others.map((part) => ({
    value: endValue(part),
    label: part.name,
  }))
  return (
    <div className="flex min-w-0 flex-col gap-4 text-xs">
      <div className="flex flex-col gap-1">
        <span className="flex items-center gap-1.5 text-2xs text-muted-foreground">
          <Icon icon={GROUP_ICON} size={14} aria-hidden="true" />
          Group
        </span>
        <h4 className="text-sm font-medium break-words text-foreground">
          {group.name}
        </h4>
        {group.description ? (
          <p className="mt-1 break-words whitespace-pre-wrap text-foreground">
            {group.description}
          </p>
        ) : (
          <p className="text-subtle">
            {canManage
              ? "No description yet: say what it is and what it's for, for agents."
              : "No description."}
          </p>
        )}
      </div>
      {canManage && (
        <div className="flex flex-wrap gap-1.5 *:flex-1">
          <Button size="sm" variant="outline" onClick={onEdit}>
            Edit
          </Button>
          <DropdownMenu>
            <DropdownMenuTrigger
              render={<Button size="sm" variant="outline" />}
            >
              <Icon icon="plus" data-icon="inline-start" />
              Add a component
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              <DropdownMenuGroup>
                {COMPONENT_KIND_ITEMS.map(({ value, label }) => (
                  <DropdownMenuItem
                    key={value}
                    onClick={() => onAddComponent(value)}
                  >
                    <Icon icon={COMPONENT_KINDS[value].icon} />
                    {label}
                  </DropdownMenuItem>
                ))}
              </DropdownMenuGroup>
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
      )}
      <section className="flex flex-col gap-1.5">
        <SectionHeading>In it</SectionHeading>
        {members.length ? (
          <ul className="-mx-1 flex flex-col">
            {members.map((part) => (
              <li key={part.id} className="flex items-center gap-1">
                <RowButton
                  onClick={() => onSelect({ type: part.type, id: part.id })}
                  lead={<PartLead part={part} />}
                  title={part.name}
                  detail={
                    part.type === "application"
                      ? "Application"
                      : componentKind(part.component)
                  }
                />
                {canManage && (
                  <Button
                    variant="ghost"
                    size="icon-xs"
                    aria-label={`Take ${part.name} out of ${group.name}`}
                    onClick={() =>
                      onGroup({ type: part.type, id: part.id }, null)
                    }
                  >
                    <Icon icon="close" />
                  </Button>
                )}
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-subtle">Nothing yet.</p>
        )}
      </section>
      {canManage && items.length > 0 && (
        <section className="flex flex-col gap-1.5">
          <label
            htmlFor={`${id}-add`}
            className="text-2xs font-medium text-muted-foreground"
          >
            Put in {group.name}
          </label>
          <Select
            items={items}
            value={null}
            onValueChange={(next) => {
              const end = next ? endFrom(String(next)) : undefined
              if (end) onGroup(end, group.id)
            }}
          >
            <SelectTrigger id={`${id}-add`} size="sm" className="w-full">
              <SelectValue placeholder="An application or component" />
            </SelectTrigger>
            <SelectContent alignItemWithTrigger={false}>
              {items.map((item) => (
                <SelectItem key={item.value} value={item.value}>
                  {item.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </section>
      )}
      <AddedFacts record={group} />
      {canManage && (
        <Button
          size="sm"
          variant="destructive"
          className="self-start"
          onClick={onRemove}
        >
          Remove group
        </Button>
      )}
    </div>
  )
}

/**
 * A selected connection: its two ends, how, where it happens in their code
 * (between two applications), and who added it.
 */
export function ConnectionDetails({
  scope,
  connection,
  parts,
  canManage,
  onSelect,
  onRemove,
  onOpenNode,
}: {
  scope: KnowledgeScope
  connection: Connection
  parts: Parts
  canManage: boolean
  onSelect: (selection: MapSelection) => void
  onRemove: (connection: Connection) => void
  onOpenNode: (repositoryId: string, nodeId: string) => void
}) {
  const kind = CONNECTION_KINDS[connection.kind]
  const source = parts.get(endOf(connection, "source").id)
  const target = parts.get(endOf(connection, "target").id)
  const end = (part: Part | undefined) =>
    part && (
      <Button
        variant="link"
        size="xs"
        className="h-auto gap-1.5 p-0"
        onClick={() => onSelect({ type: part.type, id: part.id })}
      >
        <PartLead part={part} />
        {part.name}
      </Button>
    )
  return (
    <div className="flex min-w-0 flex-col gap-4 text-xs">
      <div className="flex flex-col gap-1">
        <span className="text-2xs text-muted-foreground">
          Connection · drawn by hand
        </span>
        <h4 className="text-sm font-medium text-foreground">
          {kind?.label ?? connection.kind}
        </h4>
        {kind && <p className="text-muted-foreground">{kind.description}</p>}
      </div>
      <div className="flex flex-col items-start gap-0.5">
        {end(source)}
        <span className="text-2xs text-subtle">
          ↓ {kind?.label ?? connection.kind}
        </span>
        {end(target)}
      </div>
      {connection.description && (
        <p className="rounded-(--radius-item) border bg-muted px-2.5 py-2 break-words whitespace-pre-wrap text-foreground">
          {connection.description}
        </p>
      )}
      {source?.type === "application" && target?.type === "application" && (
        <CodeLinksSection
          key={connection.id}
          scope={scope}
          connection={connection}
          ends={{ source: source.repo, target: target.repo }}
          canManage={canManage}
          onOpenNode={onOpenNode}
        />
      )}
      <AddedFacts record={connection} />
      {canManage && (
        <Button
          size="sm"
          variant="destructive"
          className="self-start"
          onClick={() => onRemove(connection)}
        >
          Remove connection
        </Button>
      )}
    </div>
  )
}
