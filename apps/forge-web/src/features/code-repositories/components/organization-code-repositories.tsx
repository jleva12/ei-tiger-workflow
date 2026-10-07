import * as React from "react"

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
import { DropdownMenuItem } from "@/components/ui/dropdown-menu"
import { DeleteDialog } from "@/features/admin/components/delete-dialog"
import {
  ADMIN_TABLE_FEATURES,
  PIN_ACTIONS,
} from "@/features/admin/components/table-config"
import { RowMenu } from "@/features/admin/components/table-parts"
import { formatRelative } from "@/lib/format"
import { useScopeAccess } from "@/lib/hierarchy"
import {
  isActive,
  organizationCodeRepositories,
  useIngestCodeRepository,
  useOrganizationCodeRepositories,
  type CodeRepository,
  type CodeRepositoryCreate,
} from "../lib/api"
import {
  CODE_REPOSITORIES_ICON,
  failureOf,
  fullName,
  graphSize,
  statusDisplay,
} from "../lib/display"
import { AddRepositoryDialog } from "./add-repository-dialog"
import { RepositorySheet } from "./repository-sheet"

const FEATURES: Partial<DataTableFeatureConfig> = {
  ...ADMIN_TABLE_FEATURES,
  pagination: false,
  faceting: false,
}

const INITIAL_STATE: InitialTableState = { columnPinning: PIN_ACTIONS }

// The add and remove dialogs show failures themselves.
const SILENT = { meta: { silent: true } }

// Row actions reach the cells through context, so the columns stay put.
type RowActions = {
  open: (repository: CodeRepository) => void
  ingest?: (repository: CodeRepository) => void
  remove?: (repository: CodeRepository) => void
}
const ActionsContext = React.createContext<RowActions | undefined>(undefined)

const helper = createColumnHelper<CodeRepository>()
const COLUMNS = helper.columns([
  helper.accessor((repository) => fullName(repository), {
    id: "repository",
    header: "Repository",
    size: 320,
    enableHiding: false,
    meta: { label: "Repository" },
    cell: ({ row: { original: repository } }) => (
      <span className="flex min-w-0 flex-col">
        <span className="truncate text-[0.8125rem] font-medium text-foreground">
          {fullName(repository)}
        </span>
        <span className="truncate font-mono text-2xs text-muted-foreground">
          {repository.url}
        </span>
      </span>
    ),
  }),
  helper.accessor("branch", {
    header: "Branch",
    size: 140,
    meta: { label: "Branch", cellClassName: "font-mono text-2xs" },
  }),
  helper.accessor((repository) => repository.latest_ingestion?.status ?? "", {
    id: "status",
    header: "Status",
    size: 220,
    meta: { label: "Status" },
    cell: ({ row: { original: repository } }) => {
      const latest = repository.latest_ingestion
      if (!latest)
        return <span className="text-muted-foreground">Not ingested</span>
      const display = statusDisplay(latest)
      const failure = latest.status === "FAILED" ? failureOf(latest) : undefined
      return (
        <span className="flex min-w-0 items-center gap-2">
          <Chip tone={display.tone}>{display.label}</Chip>
          {failure && (
            <span
              className="truncate text-2xs text-muted-foreground"
              title={failure}
            >
              {failure}
            </span>
          )}
        </span>
      )
    },
  }),
  helper.accessor(
    (repository) => repository.last_success?.metrics?.nodes ?? -1,
    {
      id: "graph",
      header: "Code graph",
      size: 190,
      meta: { label: "Code graph", cellClassName: "text-muted-foreground" },
      cell: ({ row: { original: repository } }) =>
        graphSize(repository.last_success?.metrics) ?? "—",
    }
  ),
  helper.accessor((repository) => repository.last_success?.finished_at ?? "", {
    id: "ingested_at",
    header: "Ingested",
    size: 130,
    meta: { label: "Ingested", cellClassName: "text-muted-foreground" },
    cell: ({ getValue }) => (getValue() ? formatRelative(getValue()) : "Never"),
  }),
  helper.display({
    id: "actions",
    header: () => <span className="sr-only">Actions</span>,
    size: 64,
    enableSorting: false,
    enableHiding: false,
    enableResizing: false,
    meta: { label: "Actions", align: "right" },
    cell: ({ row }) => <RepositoryMenu repository={row.original} />,
  }),
])

function RepositoryMenu({ repository }: { repository: CodeRepository }) {
  const actions = React.useContext(ActionsContext)
  if (!actions) return null
  const busy = isActive(repository.latest_ingestion?.status)
  return (
    <RowMenu
      label={fullName(repository)}
      onEdit={() => actions.open(repository)}
      editLabel="Details…"
      editIcon={CODE_REPOSITORIES_ICON}
      onDelete={actions.remove && (() => actions.remove?.(repository))}
      deleteLabel="Remove…"
    >
      {actions.ingest && (
        <DropdownMenuItem
          disabled={busy}
          onClick={() => actions.ingest?.(repository)}
        >
          <Icon icon="refresh" />
          {busy ? "Ingestion in progress" : "Ingest now"}
        </DropdownMenuItem>
      )}
    </RowMenu>
  )
}

/**
 * An organization's code repositories page: the GitHub repositories it
 * ingests into the code graph, each with how its latest ingestion went and
 * the size of the graph its last success published. Adding one queues its
 * first ingestion; a repository opens in a side panel with every ingestion.
 * The list reads itself again every few seconds while an ingestion runs.
 */
export function OrganizationCodeRepositories({
  organizationId,
  organizationName,
  openRepository,
  onOpenRepository,
}: {
  organizationId: string
  organizationName: string
  /** The repository open in the side panel, by ID (from the URL). */
  openRepository?: string
  onOpenRepository: (id: string | undefined) => void
}) {
  const list = useOrganizationCodeRepositories(organizationId)
  const can = useScopeAccess(`org:${organizationId}`)
  const canManage = can("repositories:manage")
  const scoped = organizationCodeRepositories.scope({ organizationId })
  const create = scoped.useCreate(SILENT)
  const remove = scoped.useDelete(SILENT)
  const ingest = useIngestCodeRepository(organizationId)
  const [adding, setAdding] = React.useState(false)
  const [removing, setRemoving] = React.useState<CodeRepository>()
  const repositories = React.useMemo(() => list.data ?? [], [list.data])
  const open = repositories.find((r) => r.id === openRepository)

  const ingestMutate = ingest.mutate
  const actions = React.useMemo<RowActions>(
    () => ({
      open: (repository) => onOpenRepository(repository.id),
      ...(canManage
        ? {
            ingest: (repository: CodeRepository) =>
              ingestMutate({ repositoryId: repository.id }),
            remove: setRemoving,
          }
        : {}),
    }),
    [canManage, ingestMutate, onOpenRepository]
  )

  const added = async (input: Required<CodeRepositoryCreate>) => {
    const made = await create.mutateAsync(input)
    onOpenRepository(made.id)
  }

  return (
    <>
      {canManage && (
        <ShellHeaderActions>
          <PrimaryAction onClick={() => setAdding(true)}>
            Repository
          </PrimaryAction>
        </ShellHeaderActions>
      )}

      {list.error ? (
        <ErrorCallout
          title="Couldn't load the organization's code repositories"
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
      ) : !list.isPending && repositories.length === 0 ? (
        <EmptyWorkspace
          illustration={<EmptyIllustration name="waiting" />}
          title={`Make ${organizationName}'s code searchable`}
          description="Add a GitHub repository and Forge ingests it into the code graph: its declarations, calls, references and types, with each declaration's source. Java, TypeScript, JavaScript and Python are analysed."
          actions={
            canManage ? (
              <Button variant="outline" onClick={() => setAdding(true)}>
                Add a repository
              </Button>
            ) : undefined
          }
          steps={[
            { icon: CODE_REPOSITORIES_ICON, label: "Add the repository" },
            { icon: "refresh", label: "Forge ingests it" },
            { icon: "robot", label: "Agents search its code" },
          ]}
        />
      ) : (
        <ActionsContext.Provider value={actions}>
          <DataTable
            className="rounded-none border-0"
            title="Code repositories"
            description={`The GitHub repositories ${organizationName} ingests into the code graph. A repository's graph is shared with every organization that has it, on the branch it was first ingested on.`}
            columns={COLUMNS}
            data={repositories}
            isLoading={list.isPending}
            getRowId={(repository) => repository.id}
            features={FEATURES}
            initialState={INITIAL_STATE}
            stateKey="organization-code-repositories"
            exportFileName="code-repositories"
            labels={{ rows: "repositories", rowSingular: "repository" }}
            onRowClick={(row) => onOpenRepository(row.original.id)}
          />
        </ActionsContext.Provider>
      )}

      <AddRepositoryDialog
        open={adding}
        onOpenChange={setAdding}
        organizationName={organizationName}
        onSubmit={added}
      />

      <RepositorySheet
        organizationId={organizationId}
        repository={open}
        open={open !== undefined}
        onOpenChange={(next) => {
          if (!next) onOpenRepository(undefined)
        }}
        canManage={canManage}
        onRemove={setRemoving}
      />

      <DeleteDialog
        open={removing !== undefined}
        onClose={() => setRemoving(undefined)}
        title={`Remove ${removing ? fullName(removing) : "the repository"}?`}
        description={`It and its ingestions leave ${organizationName}; an ingestion running now stops. The code graph stays for other organizations that have the repository.`}
        confirmLabel="Remove repository"
        onConfirm={async () => {
          await remove.mutateAsync(removing!.id)
          if (openRepository === removing?.id) onOpenRepository(undefined)
        }}
      />
    </>
  )
}
