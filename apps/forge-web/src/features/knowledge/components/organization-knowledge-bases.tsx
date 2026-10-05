import * as React from "react"
import { useNavigate } from "@tanstack/react-router"
import { PencilEdit02Icon, Upload04Icon } from "@hugeicons/core-free-icons"

import { PrimaryAction } from "@/components/forge/app-shell"
import {
  createColumnHelper,
  DataTable,
  type DataTableFeatureConfig,
  type InitialTableState,
} from "@/components/forge/data-table/index"
import {
  EmptyIllustration,
  EmptyWorkspace,
} from "@/components/forge/empty-state"
import { ErrorCallout } from "@/components/forge/feedback"
import { ShellHeaderActions } from "@/components/forge/shell/index"
import { Button } from "@/components/ui/button"
import { DeleteDialog } from "@/features/admin/components/delete-dialog"
import {
  ADMIN_TABLE_FEATURES,
  PIN_ACTIONS,
} from "@/features/admin/components/table-config"
import { RowMenu } from "@/features/admin/components/table-parts"
import { formatRelative } from "@/lib/format"
import { useScopeAccess } from "@/lib/hierarchy"
import {
  organizationKnowledgeBases,
  useOrganizationKnowledgeBases,
  type KnowledgeBase,
} from "../lib/api"
import { formatBytes, formatCount, KNOWLEDGE_ICON } from "../lib/knowledge"
import { KnowledgeBaseDialog } from "./knowledge-base-dialog"

const FEATURES: Partial<DataTableFeatureConfig> = {
  ...ADMIN_TABLE_FEATURES,
  pagination: false,
  faceting: false,
}

const INITIAL_STATE: InitialTableState = { columnPinning: PIN_ACTIONS }

// The dialogs show failures themselves.
const SILENT = { meta: { silent: true } }

const plural = (count: number, one: string, many = `${one}s`) =>
  `${formatCount(count)} ${count === 1 ? one : many}`

// Row actions reach the cells through context, so the columns stay put.
type RowActions = {
  edit?: (base: KnowledgeBase) => void
  remove?: (base: KnowledgeBase) => void
}
const ActionsContext = React.createContext<RowActions | undefined>(undefined)

const helper = createColumnHelper<KnowledgeBase>()
const COLUMNS = helper.columns([
  helper.accessor("name", {
    header: "Knowledge base",
    size: 320,
    enableHiding: false,
    meta: { label: "Knowledge base" },
    cell: ({ row: { original: base } }) => (
      <span className="flex min-w-0 flex-col">
        <span className="truncate text-[0.8125rem] font-medium text-foreground">
          {base.name}
        </span>
        {base.description && (
          <span
            className="truncate text-2xs text-muted-foreground"
            title={base.description}
          >
            {base.description}
          </span>
        )}
      </span>
    ),
  }),
  helper.accessor("documents", {
    header: "Documents",
    size: 120,
    meta: { label: "Documents", align: "right" },
    cell: ({ getValue }) => (
      <span className="tabular-nums">{formatCount(getValue())}</span>
    ),
  }),
  helper.accessor("ready", {
    header: "Ready",
    size: 120,
    meta: { label: "Ready", align: "right" },
    cell: ({ row: { original: base } }) => (
      <span className="text-muted-foreground tabular-nums">
        {base.documents ? (
          <>
            {formatCount(base.ready)}
            {base.failed > 0 && (
              <span className="ml-1.5 text-danger-foreground">
                · {formatCount(base.failed)} failed
              </span>
            )}
          </>
        ) : (
          "–"
        )}
      </span>
    ),
  }),
  helper.accessor("chunks", {
    header: "Chunks",
    size: 110,
    meta: {
      label: "Chunks",
      align: "right",
      cellClassName: "text-muted-foreground",
    },
    cell: ({ getValue }) => (
      <span className="tabular-nums">{formatCount(getValue())}</span>
    ),
  }),
  helper.accessor("size_bytes", {
    header: "Size",
    size: 100,
    meta: {
      label: "Size",
      align: "right",
      cellClassName: "text-muted-foreground",
    },
    cell: ({ getValue }) => (
      <span className="tabular-nums">{formatBytes(getValue())}</span>
    ),
  }),
  helper.accessor("updated_at", {
    header: "Updated",
    size: 140,
    meta: { label: "Updated", cellClassName: "text-muted-foreground" },
    cell: ({ getValue }) => formatRelative(getValue()),
  }),
  helper.display({
    id: "actions",
    header: () => <span className="sr-only">Actions</span>,
    size: 64,
    enableSorting: false,
    enableHiding: false,
    enableResizing: false,
    meta: { label: "Actions", align: "right" },
    cell: ({ row }) => <BaseMenu base={row.original} />,
  }),
])

function BaseMenu({ base }: { base: KnowledgeBase }) {
  const actions = React.useContext(ActionsContext)
  if (!actions) return null
  return (
    <RowMenu
      label={base.name}
      onEdit={actions.edit && (() => actions.edit?.(base))}
      editLabel="Rename…"
      editIcon={PencilEdit02Icon}
      onDelete={actions.remove && (() => actions.remove?.(base))}
    />
  )
}

/**
 * An organization's knowledge bases: the sets of documents its agents can
 * search, each with how many documents it holds, how many are ready and
 * how much it stores. A knowledge base opens its own page, to upload and
 * file documents and try a search; those who manage knowledge bases
 * (`knowledge_bases:manage`) also make, rename and delete them here.
 */
export function OrganizationKnowledgeBases({
  organizationId,
  organizationName,
}: {
  organizationId: string
  organizationName: string
}) {
  const navigate = useNavigate()
  const list = useOrganizationKnowledgeBases(organizationId)
  const can = useScopeAccess(`org:${organizationId}`)
  const canManage = can("knowledge_bases:manage")
  const scoped = organizationKnowledgeBases.scope({ organizationId })
  const create = scoped.useCreate(SILENT)
  const update = scoped.useUpdate(SILENT)
  const remove = scoped.useDelete(SILENT)
  const [editing, setEditing] = React.useState<{
    open: boolean
    base?: KnowledgeBase
  }>({ open: false })
  const [removing, setRemoving] = React.useState<KnowledgeBase>()
  const bases = React.useMemo(() => list.data ?? [], [list.data])

  const open = React.useCallback(
    (id: string) =>
      void navigate({
        to: "/organizations/$organizationId/knowledge/$knowledgeBaseId",
        params: { organizationId, knowledgeBaseId: id },
      }),
    [navigate, organizationId]
  )

  const actions = React.useMemo<RowActions>(
    () =>
      canManage
        ? {
            edit: (base) => setEditing({ open: true, base }),
            remove: setRemoving,
          }
        : {},
    [canManage]
  )
  const startCreate = () => setEditing({ open: true })

  return (
    <>
      {canManage && (
        <ShellHeaderActions>
          <PrimaryAction onClick={startCreate}>Knowledge base</PrimaryAction>
        </ShellHeaderActions>
      )}

      {list.error ? (
        <ErrorCallout
          title="Couldn't load the organization's knowledge bases"
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
      ) : !list.isPending && bases.length === 0 ? (
        <EmptyWorkspace
          illustration={<EmptyIllustration name="tasks" />}
          title={`Give ${organizationName}'s agents something to read`}
          description={
            canManage
              ? "A knowledge base is a set of documents — runbooks, specs, policies — split into chunks and embedded so agents can search it. Make one, upload its documents, then attach it to the agents that need it."
              : `${organizationName} has no knowledge bases yet. Those who manage its knowledge bases make them and upload their documents.`
          }
          actions={
            canManage ? (
              <Button variant="outline" onClick={startCreate}>
                New knowledge base
              </Button>
            ) : undefined
          }
          steps={[
            { icon: KNOWLEDGE_ICON, label: "Make a knowledge base" },
            { icon: Upload04Icon, label: "Upload documents" },
            { icon: "robot", label: "Attach it to agents" },
          ]}
        />
      ) : (
        <ActionsContext.Provider value={actions}>
          <DataTable
            className="rounded-none border-0"
            title="Knowledge bases"
            description={`The documents ${organizationName}'s agents can search, in ${plural(bases.length, "knowledge base")}.`}
            columns={COLUMNS}
            data={bases}
            isLoading={list.isPending}
            getRowId={(base) => base.id}
            features={FEATURES}
            initialState={INITIAL_STATE}
            stateKey="organization-knowledge-bases"
            exportFileName="knowledge-bases"
            labels={{ rows: "knowledge bases", rowSingular: "knowledge base" }}
            onRowClick={(row) => open(row.original.id)}
          />
        </ActionsContext.Provider>
      )}

      <KnowledgeBaseDialog
        open={editing.open}
        onOpenChange={(next) =>
          setEditing((current) => ({ ...current, open: next }))
        }
        base={editing.base}
        organizationName={organizationName}
        onSubmit={async (input) => {
          if (editing.base) {
            await update.mutateAsync({ id: editing.base.id, data: input })
            return
          }
          const made = await create.mutateAsync(input)
          open(made.id)
        }}
      />

      <DeleteDialog
        open={removing !== undefined}
        onClose={() => setRemoving(undefined)}
        title={`Delete ${removing?.name ?? "the knowledge base"}?`}
        description={
          removing?.documents
            ? `Its ${plural(removing.documents, "document")} and their ${plural(removing.chunks, "chunk")} are deleted with it, and agents that use it stop finding them. This can't be undone.`
            : "It's empty. Agents that use it lose it. This can't be undone."
        }
        confirmLabel="Delete knowledge base"
        onConfirm={async () => {
          await remove.mutateAsync(removing!.id)
        }}
      />
    </>
  )
}
