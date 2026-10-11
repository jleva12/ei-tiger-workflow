import * as React from "react"

import { ErrorCallout } from "@/components/forge/feedback"
import { Icon } from "@/components/forge/icon"
import { Button } from "@/components/ui/button"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { Skeleton } from "@/components/ui/skeleton"
import { DeleteDialog } from "@/features/admin/components/delete-dialog"
import type { CodeRepository } from "@/features/code-repositories/lib/api"
import type { KnowledgeScope } from "../lib/api"
import {
  COMPONENT_KIND_ITEMS,
  COMPONENT_KINDS,
  GROUP_ICON,
  components as componentsResource,
  connections as connectionsResource,
  endOf,
  groups as groupsResource,
  touches,
  useDesignRefresh,
  useSaveSystemMapLayout,
  useSystemMapLayout,
  type Connection,
  type MapEnd,
  type SystemMapUpdate,
} from "../lib/system"
import {
  AddApplicationItems,
  type ApplicationSource,
} from "./add-application-items"
import type { MapSelection, SystemMapCanvasHandle } from "./system-map-canvas"
import {
  ComponentDialog,
  ConnectDialog,
  GroupDialog,
} from "./system-map-dialogs"
import {
  ConnectionDetails,
  GroupDetails,
  PartDetails,
  SystemOverview,
} from "./system-map-inspector"
import {
  plural,
  type Editing,
  type Part,
  type Parts,
  type Removing,
} from "../lib/map-parts"

// Cytoscape is large; load it with the map, not with the app.
const SystemMapCanvas = React.lazy(() =>
  import("./system-map-canvas").then((module) => ({
    default: module.SystemMapCanvas,
  }))
)

// The map and the inspector side by side; narrower, the map on top.
const LAYOUT =
  "grid min-h-[30rem] min-w-0 flex-1 grid-cols-[minmax(0,1fr)_clamp(18rem,28%,24rem)] grid-rows-[minmax(0,1fr)] gap-3 @max-[1050px]/shell:min-h-0 @max-[1050px]/shell:flex-none @max-[1050px]/shell:grid-cols-1 @max-[1050px]/shell:grid-rows-[26rem_auto]"

const PANEL = "min-h-0 min-w-0 rounded-(--radius-card) border bg-card"

/**
 * A system design knowledge base's map: each of its applications (code
 * repositories) and components (the parts of the system that aren't code
 * here: databases, queues, caches, storage, external systems, services,
 * clients) as a node, the groups drawn around some of them (a domain, a
 * team's services), and the connections between them (one calling
 * another's API, writing to a database, publishing to a topic) as arrows.
 * Ingestion finds none of this across applications, so those who manage
 * knowledge bases draw it here: Add puts components and groups on the
 * map, Connect joins two things, and the inspector puts each in a group.
 * Agents searching the knowledge base are told the whole design, groups'
 * descriptions included. Everyone in the organization sees the map; an
 * application opens its code graph.
 */
export function SystemMap({
  scope,
  baseName,
  organizationName,
  applications,
  canManage,
  onOpen,
  onAddApplication,
}: {
  scope: KnowledgeScope
  baseName: string
  organizationName: string
  /** The knowledge base's applications, in its list's order. */
  applications: CodeRepository[]
  /** `knowledge_bases:manage` in the organization. */
  canManage: boolean
  /** Opens an application's code graph, at a node when one's given. */
  onOpen: (repositoryId: string, nodeId?: string) => void
  /** Starts adding an application, for those who manage knowledge bases. */
  onAddApplication?: (from: ApplicationSource) => void
}) {
  const scopedConnections = connectionsResource.scope(scope)
  const list = scopedConnections.useList()
  const componentList = componentsResource.scope(scope).useList()
  const groupList = groupsResource.scope(scope).useList()
  // Where everything is and which group it's in, the same for everyone;
  // those who manage knowledge bases save what they change.
  const layout = useSystemMapLayout(scope)
  const { mutate: saveLayoutMutate } = useSaveSystemMapLayout(scope)
  const saveLayout = React.useCallback(
    (update: SystemMapUpdate) => saveLayoutMutate(update),
    [saveLayoutMutate]
  )
  const refresh = useDesignRefresh(scope)
  // The dialog shows why removing failed.
  const removeConnection = scopedConnections.useDelete({
    meta: { silent: true },
  })
  const removeComponent = componentsResource
    .scope(scope)
    .useDelete({ meta: { silent: true }, onSuccess: () => refresh() })
  const removeGroup = groupsResource
    .scope(scope)
    .useDelete({ meta: { silent: true }, onSuccess: () => refresh() })
  const canvas = React.useRef<SystemMapCanvasHandle>(null)
  const [selection, setSelection] = React.useState<MapSelection>(null)
  const [connecting, setConnecting] = React.useState(false)
  const [connectFrom, setConnectFrom] = React.useState<MapEnd>()
  // What the dialogs add or change, kept while one closes.
  const [editing, setEditing] = React.useState<Editing>()
  const [editOpen, setEditOpen] = React.useState(false)
  const edit = React.useCallback((next: Editing) => {
    setEditing(next)
    setEditOpen(true)
  }, [])
  const [removing, setRemoving] = React.useState<Removing>()

  const components = React.useMemo(
    () => componentList.data ?? [],
    [componentList.data]
  )
  const groups = React.useMemo(() => groupList.data ?? [], [groupList.data])
  const parts = React.useMemo<Parts>(
    () =>
      new Map<string, Part>([
        ...applications.map(
          (repo, index) =>
            [
              repo.id,
              {
                type: "application",
                id: repo.id,
                name: repo.name,
                color: index,
                repo,
              },
            ] as const
        ),
        ...components.map(
          (component) =>
            [
              component.id,
              {
                type: "component",
                id: component.id,
                name: component.name,
                component,
              },
            ] as const
        ),
      ]),
    [applications, components]
  )
  // A connection whose end has just left goes with it.
  const shown = React.useMemo(
    () =>
      (list.data ?? []).filter(
        (link) =>
          parts.has(endOf(link, "source").id) &&
          parts.has(endOf(link, "target").id)
      ),
    [list.data, parts]
  )
  // Which group each application and component is in, of those there are.
  const membership = React.useMemo(() => {
    const known = new Set(groups.map((group) => group.id))
    return Object.fromEntries(
      Object.entries(layout.data?.groups ?? {}).filter(
        ([id, group]) => parts.has(id) && known.has(group)
      )
    ) as Record<string, string>
  }, [layout.data, groups, parts])

  const selectedPart =
    selection?.type === "application" || selection?.type === "component"
      ? parts.get(selection.id)
      : undefined
  const selectedGroup =
    selection?.type === "group"
      ? groups.find((group) => group.id === selection.id)
      : undefined
  const selectedConnection =
    selection?.type === "connection"
      ? shown.find((link) => link.id === selection.id)
      : undefined
  // A selection that's gone (removed, or left the knowledge base) is none.
  const current =
    selectedPart || selectedGroup || selectedConnection ? selection : null

  const openApplication = React.useCallback(
    (id: string) => onOpen(id),
    [onOpen]
  )
  function connect(from?: MapEnd) {
    setConnectFrom(from)
    setConnecting(true)
  }
  /** Puts something in a group, or takes it out of its own, moving it there. */
  const setGroup = React.useCallback(
    (end: MapEnd, groupId: string | null) => {
      const point = canvas.current?.placeFor(end, groupId)
      saveLayout({
        groups: { [end.id]: groupId },
        ...(point && { positions: { [end.id]: point } }),
      })
    },
    [saveLayout]
  )

  const nameOf = (id: string) => parts.get(id)?.name ?? "the other end"
  const loading = !layout.data || !componentList.data || !groupList.data
  const failed = layout.error ?? componentList.error ?? groupList.error

  return (
    <div className="flex h-full min-w-0 flex-col gap-3 p-4 @max-[1050px]/shell:h-auto">
      <div className="flex min-h-7 items-center gap-3">
        <p className="min-w-0 truncate text-xs text-muted-foreground tabular-nums">
          {[
            plural(applications.length, "application"),
            componentList.data && plural(components.length, "component"),
            groupList.data &&
              groups.length > 0 &&
              plural(groups.length, "group"),
            list.data && plural(shown.length, "connection"),
          ]
            .filter(Boolean)
            .join(" · ")}
        </p>
        {canManage && (
          <div className="ml-auto flex items-center gap-1.5">
            <AddMenu
              organizationName={organizationName}
              onAddApplication={onAddApplication}
              onAdd={edit}
            />
            <Button
              size="sm"
              variant="outline"
              disabled={parts.size < 2}
              onClick={() =>
                connect(
                  selectedPart && {
                    type: selectedPart.type,
                    id: selectedPart.id,
                  }
                )
              }
            >
              <Icon icon="link" data-icon="inline-start" />
              Connect
            </Button>
          </div>
        )}
      </div>
      {list.error && !list.data && (
        <ErrorCallout
          title="Couldn't load the connections"
          action={
            <Button
              variant="outline"
              size="sm"
              onClick={() => void list.refetch()}
            >
              Retry
            </Button>
          }
        >
          {list.error.message}
        </ErrorCallout>
      )}
      <div className={LAYOUT}>
        <div className={`${PANEL} overflow-hidden`}>
          {loading ? (
            failed ? (
              <div className="flex size-full items-center justify-center p-6">
                <ErrorCallout
                  title="Couldn't load the map"
                  action={
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() => {
                        void layout.refetch()
                        void componentList.refetch()
                        void groupList.refetch()
                      }}
                    >
                      Retry
                    </Button>
                  }
                >
                  {failed.message}
                </ErrorCallout>
              </div>
            ) : (
              <Skeleton className="size-full rounded-none" />
            )
          ) : (
            <React.Suspense
              fallback={<Skeleton className="size-full rounded-none" />}
            >
              <SystemMapCanvas
                ref={canvas}
                saved={layout.data!}
                onSave={canManage ? saveLayout : undefined}
                applications={applications}
                components={components}
                groups={groups}
                connections={shown}
                selection={current}
                onSelect={setSelection}
                onOpen={openApplication}
              />
            </React.Suspense>
          )}
        </div>
        <aside
          className={`${PANEL} flex flex-col overflow-hidden`}
          aria-label="Inspector"
        >
          <header className="flex h-10 shrink-0 items-center justify-between border-b pr-1.5 pl-3">
            <h3 className="text-xs font-medium text-foreground">
              {selectedPart?.type === "application"
                ? "Application"
                : selectedPart?.type === "component"
                  ? "Component"
                  : selectedGroup
                    ? "Group"
                    : selectedConnection
                      ? "Connection"
                      : "System"}
            </h3>
            {current && (
              <Button
                variant="ghost"
                size="icon-xs"
                aria-label="Back to the whole system"
                onClick={() => setSelection(null)}
              >
                <Icon icon="close" />
              </Button>
            )}
          </header>
          <div
            tabIndex={0}
            className="min-h-0 flex-1 overflow-y-auto p-3 @max-[1050px]/shell:max-h-96"
          >
            {selectedPart ? (
              <PartDetails
                key={selectedPart.id}
                baseName={baseName}
                part={selectedPart}
                connections={shown}
                parts={parts}
                groups={groups}
                group={membership[selectedPart.id]}
                canManage={canManage}
                alone={parts.size < 2}
                onConnect={() =>
                  connect({ type: selectedPart.type, id: selectedPart.id })
                }
                onGroup={setGroup}
                onOpen={openApplication}
                onEdit={(component) =>
                  edit({ type: "component", kind: component.kind, component })
                }
                onSelect={setSelection}
                onRemove={setRemoving}
              />
            ) : selectedGroup ? (
              <GroupDetails
                key={selectedGroup.id}
                group={selectedGroup}
                parts={parts}
                membership={membership}
                canManage={canManage}
                onGroup={setGroup}
                onAddComponent={(kind) =>
                  edit({ type: "component", kind, groupId: selectedGroup.id })
                }
                onEdit={() => edit({ type: "group", group: selectedGroup })}
                onSelect={setSelection}
                onRemove={() =>
                  setRemoving({ type: "group", group: selectedGroup })
                }
              />
            ) : selectedConnection ? (
              <ConnectionDetails
                scope={scope}
                connection={selectedConnection}
                parts={parts}
                canManage={canManage}
                onSelect={setSelection}
                onRemove={(connection) =>
                  setRemoving({ type: "connection", connection })
                }
                onOpenNode={onOpen}
              />
            ) : (
              <SystemOverview
                baseName={baseName}
                applications={applications}
                components={components}
                groups={groups}
                membership={membership}
                connections={shown}
                canManage={canManage}
                onSelect={setSelection}
              />
            )}
          </div>
        </aside>
      </div>
      {canManage && (
        <>
          <ConnectDialog
            open={connecting}
            onOpenChange={setConnecting}
            scope={scope}
            parts={parts}
            source={connectFrom}
            onConnected={(link) =>
              setSelection({ type: "connection", id: link.id })
            }
          />
          <ComponentDialog
            open={editOpen && editing?.type === "component"}
            onOpenChange={setEditOpen}
            scope={scope}
            baseName={baseName}
            editing={editing?.type === "component" ? editing : undefined}
            groups={groups}
            onSaved={(component) =>
              setSelection({ type: "component", id: component.id })
            }
          />
          <GroupDialog
            open={editOpen && editing?.type === "group"}
            onOpenChange={setEditOpen}
            scope={scope}
            baseName={baseName}
            group={editing?.type === "group" ? editing.group : undefined}
            onSaved={(group) => setSelection({ type: "group", id: group.id })}
          />
        </>
      )}
      <DeleteDialog
        open={removing !== undefined}
        onClose={() => setRemoving(undefined)}
        {...removalCopy(removing, nameOf, shown)}
        onConfirm={() =>
          removing?.type === "connection"
            ? removeConnection.mutateAsync(removing.connection.id)
            : removing?.type === "component"
              ? removeComponent.mutateAsync(removing.component.id)
              : removeGroup.mutateAsync(removing!.group.id)
        }
      />
    </div>
  )
}

/** What the removal dialog says for what's being removed. */
function removalCopy(
  removing: Removing | undefined,
  nameOf: (id: string) => string,
  connections: Connection[]
) {
  if (removing?.type === "component") {
    const count = connections.filter((link) =>
      touches(link, removing.component.id)
    ).length
    return {
      title: `Remove ${removing.component.name}?`,
      description: count
        ? `It's taken off the system map with its ${plural(count, "connection")}, and agents stop being told about it.`
        : "It's taken off the system map, and agents stop being told about it.",
      confirmLabel: "Remove component",
    }
  }
  if (removing?.type === "group") {
    return {
      title: `Remove the group ${removing.group.name}?`,
      description:
        "What's in it stays on the system map, in no group, with its connections.",
      confirmLabel: "Remove group",
    }
  }
  const link = removing?.connection
  return {
    title: `Remove the connection from ${link ? nameOf(endOf(link, "source").id) : ""} to ${link ? nameOf(endOf(link, "target").id) : ""}?`,
    description:
      "It's taken off the system map, with its code links; nothing changes at either end.",
    confirmLabel: "Remove connection",
  }
}

/** The toolbar's Add: an application, a component of a kind, or a group. */
function AddMenu({
  organizationName,
  onAddApplication,
  onAdd,
}: {
  organizationName: string
  onAddApplication?: (from: ApplicationSource) => void
  onAdd: (editing: Editing) => void
}) {
  return (
    <DropdownMenu>
      <DropdownMenuTrigger render={<Button size="sm" variant="outline" />}>
        <Icon icon="plus" data-icon="inline-start" />
        Add
        <Icon icon="down" data-icon="inline-end" />
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-80">
        {onAddApplication && (
          <>
            <AddApplicationItems
              organizationName={organizationName}
              onPick={onAddApplication}
            />
            <DropdownMenuSeparator />
          </>
        )}
        <DropdownMenuGroup>
          <DropdownMenuLabel>Add a component…</DropdownMenuLabel>
          {COMPONENT_KIND_ITEMS.map(({ value, label }) => (
            <DropdownMenuItem
              key={value}
              onClick={() => onAdd({ type: "component", kind: value })}
            >
              <Icon icon={COMPONENT_KINDS[value].icon} />
              {label}
            </DropdownMenuItem>
          ))}
        </DropdownMenuGroup>
        <DropdownMenuSeparator />
        <DropdownMenuGroup>
          <DropdownMenuItem
            onClick={() => onAdd({ type: "group" })}
            className="items-start"
          >
            <Icon icon={GROUP_ICON} className="mt-0.5" />
            <span className="flex min-w-0 flex-col">
              <span className="text-foreground">A group</span>
              <span className="text-2xs text-muted-foreground">
                Around applications and components: a domain, a team's services
              </span>
            </span>
          </DropdownMenuItem>
        </DropdownMenuGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
