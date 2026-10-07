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
import { Icon } from "@/components/forge/icon"
import { ShellHeaderActions } from "@/components/forge/shell/index"
import { Chip } from "@/components/forge/status"
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
  type KnowledgeBaseKind,
} from "../lib/api"
import { formatBytes, formatCount, KNOWLEDGE_ICON } from "../lib/knowledge"
import { KnowledgeBaseDialog } from "./knowledge-base-dialog"
import { KNOWLEDGE_BASE_KINDS } from "../lib/kinds"
import { NewKnowledgeBaseMenu } from "./new-knowledge-base-menu"

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

// What a knowledge base holds, and how much of it is searchable: its
// documents, or a graph one's repositories.
const contentsOf = (base: KnowledgeBase) =>
  base.kind === "graph" ? base.repositories : base.documents
const readyOf = (base: KnowledgeBase) =>
  base.kind === "graph" ? base.ingested : base.ready

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
  helper.accessor("kind", {
    header: "Type",
    size: 110,
    meta: { label: "Type" },
    cell: ({ getValue }) => {
      const info = KNOWLEDGE_BASE_KINDS[getValue()]
      return <Chip icon={info.icon}>{info.short}</Chip>
    },
  }),
  helper.accessor(contentsOf, {
    id: "contents",
    header: "Contents",
    size: 150,
    meta: { label: "Contents", align: "right" },
    cell: ({ row: { original: base } }) => (
      <span className="tabular-nums">
        {base.kind === "graph"
          ? plural(base.repositories, "repository", "repositories")
          : plural(base.documents, "document")}
      </span>
    ),
  }),
  helper.accessor(readyOf, {
    id: "ready",
    header: "Ready",
    size: 140,
    meta: { label: "Ready", align: "right" },
    cell: ({ row: { original: base } }) => (
      <span className="text-muted-foreground tabular-nums">
        {base.kind === "graph" ? (
          base.repositories ? (
            `${formatCount(base.ingested)} ingested`
          ) : (
            "–"
          )
        ) : base.documents ? (
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
    cell: ({ row: { original: base }, getValue }) => (
      <span className="tabular-nums">
        {base.kind === "graph" ? "–" : formatCount(getValue())}
      </span>
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
    cell: ({ row: { original: base }, getValue }) => (
      <span className="tabular-nums">
        {base.kind === "graph" ? "–" : formatBytes(getValue())}
      </span>
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
 * An organization's knowledge bases: what its agents can search. A RAG one
 * holds documents, with how many it holds, how many are ready and how much
 * it stores; a graph one holds code repositories, with how many are
 * ingested into the code graph. A knowledge base opens its own page, to
 * upload documents or add repositories and try a search; those who manage
 * knowledge bases (`knowledge_bases:manage`) also make one of either kind
 * (the action's menu), rename and delete them here.
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
    kind?: KnowledgeBaseKind
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
  const startCreate = (kind: KnowledgeBaseKind) =>
    setEditing({ open: true, kind })

  return (
    <>
      {canManage && (
        <ShellHeaderActions>
          <NewKnowledgeBaseMenu
            trigger={<PrimaryAction />}
            onPick={startCreate}
          >
            Knowledge base
          </NewKnowledgeBaseMenu>
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
              ? "A knowledge base is what agents search: a set of documents — runbooks, specs, policies — split into chunks and embedded (RAG), or code repositories ingested into the code graph (graph). Make one, fill it, then attach it to the agents that need it."
              : `${organizationName} has no knowledge bases yet. Those who manage its knowledge bases make them and upload their documents.`
          }
          actions={
            canManage ? (
              <NewKnowledgeBaseMenu
                trigger={<Button variant="outline" />}
                onPick={startCreate}
              >
                New knowledge base
                <Icon icon="down" data-icon="inline-end" />
              </NewKnowledgeBaseMenu>
            ) : undefined
          }
          steps={[
            { icon: KNOWLEDGE_ICON, label: "Make a knowledge base" },
            { icon: Upload04Icon, label: "Upload documents or add code" },
            { icon: "robot", label: "Attach it to agents" },
          ]}
        />
      ) : (
        <ActionsContext.Provider value={actions}>
          <DataTable
            className="rounded-none border-0"
            title="Knowledge bases"
            description={`What ${organizationName}'s agents can search, in ${plural(bases.length, "knowledge base")}: documents, and code.`}
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
        kind={editing.kind}
        organizationName={organizationName}
        onSubmit={async (input) => {
          if (editing.base) {
            await update.mutateAsync({ id: editing.base.id, data: input })
            return
          }
          const made = await create.mutateAsync({
            ...input,
            kind: editing.kind ?? "rag",
          })
          open(made.id)
        }}
      />

      <DeleteDialog
        open={removing !== undefined}
        onClose={() => setRemoving(undefined)}
        title={`Delete ${removing?.name ?? "the knowledge base"}?`}
        description={
          removing?.kind === "graph"
            ? removing.repositories
              ? `Its ${plural(removing.repositories, "repository", "repositories")} stay in ${organizationName}, with their code graphs; agents that use it stop searching them. This can't be undone.`
              : "It's empty. Agents that use it lose it. This can't be undone."
            : removing?.documents
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
