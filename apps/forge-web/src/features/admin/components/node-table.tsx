import * as React from "react"

import {
  createColumnHelper,
  DataTable,
  type DataTableFeatureConfig,
  type DataTableProps,
  type InitialTableState,
  type Row,
} from "@/components/forge/data-table/index"
import { ErrorCallout } from "@/components/forge/feedback"
import { Icon } from "@/components/forge/icon"
import { Illustration } from "@/components/forge/illustration"
import { Button } from "@/components/ui/button"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import type { ApiError } from "@/lib/api/index"
import type { NodeInput, Organization, PermissionKey } from "@/lib/hierarchy"
import { useActorLabels } from "@/lib/users"
import type { Level } from "./levels"
import { DeleteNodeDialog, NodeDialog } from "./node-dialogs"

/** The parts of a list query the table reads. */
export type NodeListQuery<T> = {
  data: T[] | undefined
  error: ApiError | null
  isPending: boolean
  refetch: () => unknown
}

type RowActionHandlers = {
  onOpen?: (node: Organization) => void
  onEdit?: (node: Organization) => void
  onDelete?: (node: Organization) => void
  /** Extra `DropdownMenuItem`s for a row, after Open. */
  rowActions?: (node: Organization) => React.ReactNode
}

// The action handlers reach the cells through context, so the columns stay
// the same from render to render.
const RowActionsContext = React.createContext<RowActionHandlers>({})

const FEATURES: Partial<DataTableFeatureConfig> = {
  columnDragging: false,
  columnFilters: false,
  editing: false,
  expanding: false,
  grouping: false,
  rowPinning: false,
  rowSelection: false,
}

const INITIAL_STATE: InitialTableState = {
  columnVisibility: { created_at: false, created_by: false },
  // Keeps the row menu in view when the table scrolls sideways.
  columnPinning: { start: [], end: ["actions"] },
}

const DATE_TIME = { dateStyle: "medium", timeStyle: "short" } as const

/** Columns a level adds to the common ones, e.g. counts of what's below. */
export type NodeColumns<T extends Organization> = DataTableProps<T>["columns"]

function nodeColumns<T extends Organization>(
  level: Level,
  withActions: boolean,
  extra: NodeColumns<T> = []
) {
  const helper = createColumnHelper<T>()
  const actions = helper.display({
    id: "actions",
    header: () => <span className="sr-only">Actions</span>,
    size: 64,
    enableSorting: false,
    enableHiding: false,
    enableResizing: false,
    meta: { label: "Actions", align: "right" },
    cell: ({ row }) => <RowActions node={row.original} level={level} />,
  })
  return helper.columns([
    helper.accessor((node) => node.name, {
      id: "name",
      header: "Name",
      size: 240,
      meta: { label: "Name" },
      cell: ({ row }) => (
        <span className="flex min-w-0 items-center gap-2 font-medium text-foreground">
          <Icon
            icon={level.icon}
            size={16}
            className="shrink-0 text-muted-foreground"
          />
          <span className="truncate">{row.original.name}</span>
        </span>
      ),
    }),
    helper.accessor((node) => node.description, {
      id: "description",
      header: "Description",
      size: 280,
      meta: { label: "Description", cellClassName: "text-muted-foreground" },
      cell: ({ getValue }) =>
        getValue() || <span className="text-subtle">—</span>,
    }),
    ...extra,
    helper.accessor((node) => node.updated_at, {
      id: "updated_at",
      header: "Updated",
      size: 170,
      meta: { label: "Updated", format: "datetime", formatOptions: DATE_TIME },
    }),
    helper.accessor((node) => node.updated_by, {
      id: "updated_by",
      header: "Updated by",
      size: 160,
      meta: { label: "Updated by", cellClassName: "text-muted-foreground" },
    }),
    helper.accessor((node) => node.created_at, {
      id: "created_at",
      header: "Created",
      size: 170,
      meta: { label: "Created", format: "datetime", formatOptions: DATE_TIME },
    }),
    helper.accessor((node) => node.created_by, {
      id: "created_by",
      header: "Created by",
      size: 160,
      meta: { label: "Created by", cellClassName: "text-muted-foreground" },
    }),
    ...(withActions ? [actions] : []),
  ])
}

function RowActions({ node, level }: { node: Organization; level: Level }) {
  const { onOpen, onEdit, onDelete, rowActions } =
    React.useContext(RowActionsContext)
  return (
    <DropdownMenu>
      <DropdownMenuTrigger
        render={
          <Button
            variant="ghost"
            size="icon-xs"
            aria-label={`Actions for ${node.name}`}
          />
        }
      >
        <Icon icon="more" />
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="min-w-40">
        {onOpen && (
          <DropdownMenuItem onClick={() => onOpen(node)}>
            <Icon icon="right" />
            Open {level.noun}
          </DropdownMenuItem>
        )}
        {rowActions?.(node)}
        {onEdit && (
          <DropdownMenuItem onClick={() => onEdit(node)}>
            <Icon icon="settings" />
            Edit…
          </DropdownMenuItem>
        )}
        {onDelete && (
          <>
            {(onOpen || onEdit || rowActions) && <DropdownMenuSeparator />}
            <DropdownMenuItem
              variant="destructive"
              onClick={() => onDelete(node)}
            >
              <Icon icon="close" />
              Delete…
            </DropdownMenuItem>
          </>
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

function NoNodes({ level, onCreate }: { level: Level; onCreate?: () => void }) {
  return (
    <div className="flex flex-col items-center gap-3 py-4 text-xs text-subtle">
      <Illustration name="tasks" size="sm" />
      <p>No {level.plural.toLowerCase()} yet.</p>
      {onCreate && (
        <Button variant="outline" size="sm" onClick={onCreate}>
          <Icon icon="plus" data-icon="inline-start" />
          New {level.noun}
        </Button>
      )}
    </div>
  )
}

type NodeTableProps<T extends Organization> = RowActionHandlers & {
  level: Level
  list: NodeListQuery<T>
  /** Added after the description; define them at module scope. */
  columns?: NodeColumns<T>
  title?: string
  description?: string
  /** Offered in the empty state. */
  onCreate?: () => void
  /**
   * Makes rows expandable, showing this under each. Keep it stable (e.g.
   * `useCallback`) and let what it renders read its own data.
   */
  renderDetail?: (node: T) => React.ReactNode
}

/** A level's nodes in the Forge data table: search, sort, columns, export. */
export function NodeTable<T extends Organization>({
  level,
  list,
  columns: extraColumns,
  title,
  description,
  onCreate,
  onOpen,
  onEdit,
  onDelete,
  rowActions,
  renderDetail,
}: NodeTableProps<T>) {
  const withActions = Boolean(onEdit || onDelete || rowActions)
  const columns = React.useMemo(
    () => nodeColumns(level, withActions, extraColumns),
    [level, withActions, extraColumns]
  )
  const handlers = React.useMemo(
    () => ({ onOpen, onEdit, onDelete, rowActions }),
    [onOpen, onEdit, onDelete, rowActions]
  )
  const features = React.useMemo(
    () => ({ ...FEATURES, expanding: Boolean(renderDetail) }),
    [renderDetail]
  )
  const renderSubComponent = React.useMemo(
    () => renderDetail && ((row: Row<T>) => renderDetail(row.original)),
    [renderDetail]
  )
  // Who created and updated each one, by email rather than user ID.
  const rows = useActorLabels(list.data)

  if (list.error && !list.data) {
    return (
      <ErrorCallout
        title={`Couldn't load the ${level.plural.toLowerCase()}`}
        action={
          <Button variant="outline" size="sm" onClick={() => list.refetch()}>
            Retry
          </Button>
        }
      >
        {list.error.message}
      </ErrorCallout>
    )
  }

  return (
    <RowActionsContext.Provider value={handlers}>
      <DataTable
        className="rounded-none border-0"
        title={title}
        description={description}
        columns={columns}
        data={rows}
        isLoading={list.isPending}
        getRowId={(node) => node.id}
        onRowClick={onOpen && ((row) => onOpen(row.original))}
        renderSubComponent={renderSubComponent}
        features={features}
        initialState={INITIAL_STATE}
        stateKey={`admin-${level.resource}`}
        exportFileName={level.plural.toLowerCase()}
        labels={{ searchPlaceholder: `Search ${level.plural.toLowerCase()}…` }}
        // With rows, an empty table means the search matched nothing.
        emptyState={
          rows.length === 0 ? (
            <NoNodes level={level} onCreate={onCreate} />
          ) : undefined
        }
      />
    </RowActionsContext.Provider>
  )
}

type ChildNodesProps<T extends Organization> = {
  level: Level
  list: NodeListQuery<T>
  /** Added after the description; define them at module scope. */
  columns?: NodeColumns<T>
  /** What the user may do in the parent's scope, which covers its children. */
  can: (permission: PermissionKey) => boolean
  /** Whether the create dialog is open; the page's primary action opens it. */
  creating: boolean
  onCreatingChange: (open: boolean) => void
  /** The parent's name, for the create dialog. */
  parentName?: string
  create: (input: NodeInput) => Promise<unknown>
  update: (id: string, input: NodeInput) => Promise<unknown>
  remove: (id: string) => Promise<unknown>
  /** Opens a node's page; leave it out for a level without pages. */
  onOpen?: (node: T) => void
  /** Extra `DropdownMenuItem`s for a row's menu; keep it stable. */
  rowActions?: (node: T) => React.ReactNode
  /** Makes rows expandable; see `NodeTable`. */
  renderDetail?: (node: T) => React.ReactNode
  title?: string
  description?: string
}

/**
 * The nodes under a parent, with the dialogs that create, edit and delete
 * them. Actions the user's roles don't grant aren't offered.
 */
export function ChildNodes<T extends Organization>({
  level,
  list,
  columns,
  can,
  creating,
  onCreatingChange,
  parentName,
  create,
  update,
  remove,
  onOpen,
  rowActions,
  renderDetail,
  title,
  description,
}: ChildNodesProps<T>) {
  const [editing, setEditing] = React.useState<Organization>()
  const [deleting, setDeleting] = React.useState<Organization>()
  const canCreate = can(`${level.resource}:create`)
  const canUpdate = can(`${level.resource}:update`)
  const canDelete = can(`${level.resource}:delete`)

  return (
    <>
      <NodeTable
        level={level}
        list={list}
        columns={columns}
        title={title}
        description={description}
        onCreate={canCreate ? () => onCreatingChange(true) : undefined}
        onOpen={onOpen as ((node: Organization) => void) | undefined}
        rowActions={
          rowActions as ((node: Organization) => React.ReactNode) | undefined
        }
        renderDetail={renderDetail}
        onEdit={canUpdate ? setEditing : undefined}
        onDelete={canDelete ? setDeleting : undefined}
      />
      <NodeDialog
        level={level}
        open={creating}
        onOpenChange={onCreatingChange}
        parentName={parentName}
        onSubmit={(input) => create(input)}
      />
      <NodeDialog
        level={level}
        open={editing !== undefined}
        onOpenChange={(open) => !open && setEditing(undefined)}
        node={editing}
        onSubmit={(input, node) => update(node!.id, input)}
      />
      <DeleteNodeDialog
        level={level}
        node={deleting}
        onClose={() => setDeleting(undefined)}
        onConfirm={(node) => remove(node.id)}
      />
    </>
  )
}
