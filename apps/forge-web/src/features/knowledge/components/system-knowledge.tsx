import * as React from "react"
import { Link } from "@tanstack/react-router"
import { GithubIcon } from "@hugeicons/core-free-icons"

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
  PageEmpty,
} from "@/components/forge/empty-state"
import { ErrorCallout } from "@/components/forge/feedback"
import { Icon } from "@/components/forge/icon"
import type { IconProp } from "@/components/forge/icons"
import {
  ShellHeaderActions,
  ShellSidebarHeader,
  ShellSidebarTop,
  useShellPage,
} from "@/components/forge/shell"
import { Chip } from "@/components/forge/status"
import {
  NavItem,
  NavSectionHeading,
  SidebarSection,
} from "@/components/forge/workspace-sidebar"
import { Button } from "@/components/ui/button"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { Skeleton } from "@/components/ui/skeleton"
import { DeleteDialog } from "@/features/admin/components/delete-dialog"
import {
  ADMIN_TABLE_FEATURES,
  PIN_ACTIONS,
} from "@/features/admin/components/table-config"
import { RowMenu } from "@/features/admin/components/table-parts"
import {
  RepositoryGraphPage,
  type RepositoryGraphSection,
} from "@/features/code-graph/components/repository-graph-page"
import { AddRepositoryDialog } from "@/features/code-repositories/components/add-repository-dialog"
import {
  isActive,
  useIngestCodeRepository,
  useOrganizationCodeRepositories,
  type CodeRepository,
} from "@/features/code-repositories/lib/api"
import {
  CODE_REPOSITORIES_ICON,
  failureOf,
  fullName,
  graphSize,
  statusDisplay,
} from "@/features/code-repositories/lib/display"
import { formatRelative } from "@/lib/format"
import { useScopeAccess } from "@/lib/hierarchy"
import type { KnowledgeBase, KnowledgeScope } from "../lib/api"
import { KNOWLEDGE_ICON, type KnowledgeSearch } from "../lib/knowledge"
import {
  useKnowledgeBaseRepositories,
  useKnowledgeBaseRepositoryMutations,
} from "../lib/repositories"
import { SYSTEM_ICON } from "../lib/system"
import { IncludeRepositoryDialog } from "./include-repository-dialog"
import type { SearchChange } from "./knowledge-page"
import { KnowledgeSearchDialog } from "./knowledge-search"
import { SystemMap } from "./system-map"

const FEATURES: Partial<DataTableFeatureConfig> = {
  ...ADMIN_TABLE_FEATURES,
  pagination: false,
  faceting: false,
}

const INITIAL_STATE: InitialTableState = { columnPinning: PIN_ACTIONS }

/** A repository in the sub nav: what its latest ingestion is doing. */
function statusIcon(repository: CodeRepository): IconProp {
  switch (repository.latest_ingestion?.status) {
    case "QUEUED":
      return "clock"
    case "RUNNING":
      return "loading"
    case "FAILED":
      return "failed"
    case undefined:
      return CODE_REPOSITORIES_ICON
    default:
      return "completed"
  }
}

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
    header: "Application",
    size: 300,
    enableHiding: false,
    meta: { label: "Application" },
    cell: ({ row: { original: repository } }) => (
      <span className="flex min-w-0 flex-col">
        <span className="truncate text-[0.8125rem] font-medium text-foreground">
          {fullName(repository)}
        </span>
        <span className="truncate font-mono text-2xs text-muted-foreground">
          {repository.branch}
        </span>
      </span>
    ),
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
      editLabel="Open code graph"
      editIcon="layers"
      onDelete={actions.remove && (() => actions.remove?.(repository))}
      deleteLabel="Remove from the system…"
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
 * A system design knowledge base's page: a system's applications (some of
 * the organization's code repositories), how they connect, and their code
 * graphs, which its agents search. It opens on the system map, where those
 * who manage knowledge bases draw the connections between applications
 * (what calls what, what sends messages to what) and say where in the code
 * each happens; All applications (`show=applications`) tables them with
 * their graphs' sizes. Its own sub nav lists them, each with how its latest
 * ingestion stands; one opens its code graph to explore (`?repository=`, at
 * `node`) and its Ingestion tab (`section=ingestion`), auditing what its
 * ingestions put in the graph. Those who manage knowledge bases add the
 * organization's repositories, or a GitHub repository it doesn't have yet
 * (adding it to the organization, with its first ingestion queued), and
 * remove them, which leaves them in the organization. Ingesting takes
 * `repositories:manage`. Everyone tries a search as agents would. The list
 * reads itself again every few seconds while an ingestion runs.
 */
export function SystemKnowledge({
  organizationId,
  organizationName,
  base,
  search,
  onSearch,
}: {
  organizationId: string
  organizationName: string
  base: KnowledgeBase
  search: KnowledgeSearch
  onSearch: SearchChange
}) {
  const scope = React.useMemo<KnowledgeScope>(
    () => ({ organizationId, knowledgeBaseId: base.id }),
    [organizationId, base.id]
  )
  const can = useScopeAccess(`org:${organizationId}`)
  const canManage = can("knowledge_bases:manage")
  const canIngest = can("repositories:manage")
  const list = useKnowledgeBaseRepositories(scope)
  const { include, remove } = useKnowledgeBaseRepositoryMutations(scope)
  const ingest = useIngestCodeRepository(organizationId)
  const [adding, setAdding] = React.useState<"existing" | "new">()
  const [removing, setRemoving] = React.useState<CodeRepository>()
  const [testing, setTesting] = React.useState(false)
  const repositories = React.useMemo(() => list.data ?? [], [list.data])
  const open = search.repository
    ? repositories.find((r) => r.id === search.repository)
    : undefined

  // The organization's repositories the knowledge base doesn't have yet, to
  // pick from; read only once the picker opens.
  const organizationRepositories = useOrganizationCodeRepositories(
    organizationId,
    { enabled: adding === "existing" }
  )
  const candidates = React.useMemo(() => {
    if (!organizationRepositories.data) return undefined
    const included = new Set(repositories.map((r) => r.id))
    return organizationRepositories.data.filter((r) => !included.has(r.id))
  }, [organizationRepositories.data, repositories])

  const basePath = `/organizations/${encodeURIComponent(organizationId)}/knowledge/${encodeURIComponent(base.id)}`
  useShellPage(
    open
      ? {
          header: {
            title: fullName(open),
            icon: CODE_REPOSITORIES_ICON,
            breadcrumbs: [
              {
                label: organizationName,
                href: `/organizations/${organizationId}`,
              },
              {
                label: "Knowledge bases",
                href: `/organizations/${organizationId}?view=knowledge`,
              },
              { label: base.name, href: basePath },
            ],
          },
        }
      : {}
  )

  const openRepository = React.useCallback(
    (repository: CodeRepository | undefined) =>
      onSearch({
        repository: repository?.id,
        section: undefined,
        node: undefined,
      }),
    [onSearch]
  )
  const openAt = React.useCallback(
    (repositoryId: string, nodeId?: string) =>
      onSearch({
        repository: repositoryId,
        section: undefined,
        node: nodeId,
        show: undefined,
      }),
    [onSearch]
  )
  const openView = (view: KnowledgeSearch["show"]) =>
    onSearch({
      repository: undefined,
      section: undefined,
      node: undefined,
      show: view,
    })
  const tabled = !open && search.show === "applications"
  const actions = React.useMemo<RowActions>(
    () => ({
      open: openRepository,
      ingest: canIngest
        ? (repository) => ingest.mutate({ repositoryId: repository.id })
        : undefined,
      remove: canManage ? setRemoving : undefined,
    }),
    [canIngest, canManage, ingest, openRepository]
  )

  const nav = (
    <>
      <ShellSidebarHeader>
        <SidebarSection variant="primary">
          <NavItem
            icon="left"
            render={
              <Link
                to="/organizations/$organizationId"
                params={{ organizationId }}
                search={{ view: "knowledge" }}
              />
            }
          >
            <span className="truncate">Knowledge bases</span>
          </NavItem>
          <NavItem
            icon={SYSTEM_ICON}
            active={!open && !tabled}
            onClick={() => openView(undefined)}
          >
            System map
          </NavItem>
          <NavItem
            icon={KNOWLEDGE_ICON}
            active={tabled}
            meta={repositories.length || undefined}
            onClick={() => openView("applications")}
          >
            All applications
          </NavItem>
        </SidebarSection>
      </ShellSidebarHeader>
      <ShellSidebarTop>
        <SidebarSection>
          <NavSectionHeading
            action={
              canManage ? (
                <AddMenu
                  trigger={
                    <Button
                      variant="ghost"
                      size="icon-xs"
                      aria-label="Add an application"
                    />
                  }
                  organizationName={organizationName}
                  onPick={setAdding}
                >
                  <Icon icon="plus" />
                </AddMenu>
              ) : undefined
            }
          >
            Applications
          </NavSectionHeading>
          {list.isPending ? (
            <div className="grid gap-1.5 px-2.5 py-1" aria-busy="true">
              <Skeleton className="h-5 w-40" />
              <Skeleton className="h-5 w-32" />
            </div>
          ) : repositories.length === 0 ? (
            <p className="px-2.5 py-1 text-xs text-subtle">
              No applications yet.
            </p>
          ) : (
            repositories.map((repository) => (
              <NavItem
                key={repository.id}
                icon={statusIcon(repository)}
                active={open?.id === repository.id}
                title={`${fullName(repository)} · ${repository.branch}`}
                onClick={() => openRepository(repository)}
              >
                <span className="truncate">{fullName(repository)}</span>
              </NavItem>
            ))
          )}
        </SidebarSection>
      </ShellSidebarTop>
    </>
  )

  return (
    <>
      {nav}
      <ShellHeaderActions>
        <Button variant="outline" onClick={() => setTesting(true)}>
          <Icon icon="search" data-icon="inline-start" />
          Test search
        </Button>
        {canManage && (
          <AddMenu
            trigger={<PrimaryAction />}
            organizationName={organizationName}
            onPick={setAdding}
          >
            Application
          </AddMenu>
        )}
      </ShellHeaderActions>

      {list.error ? (
        <ErrorCallout
          title="Couldn't load the knowledge base's applications"
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
      ) : open ? (
        <RepositoryGraphPage
          key={open.id}
          organizationId={organizationId}
          repository={open}
          openAt={search.node}
          canIngest={canIngest}
          section={(search.section ?? "graph") as RepositoryGraphSection}
          onSectionChange={(section) =>
            onSearch(
              { section: section === "ingestion" ? "ingestion" : undefined },
              { replace: true }
            )
          }
        />
      ) : search.repository && !list.isPending ? (
        <PageEmpty
          illustration="search"
          title="This application isn't in the knowledge base anymore"
          description="It was removed, or the link is wrong."
        >
          <Button variant="outline" onClick={() => openRepository(undefined)}>
            System map
          </Button>
        </PageEmpty>
      ) : !list.isPending && repositories.length === 0 ? (
        <EmptyWorkspace
          illustration={<EmptyIllustration name="tasks" />}
          title={`Map ${base.name}'s applications`}
          description={
            canManage
              ? `A system design knowledge base holds a system's applications — code repositories ingested into the code graph — and how they connect: what calls what, what sends messages to what. Agents searching ${base.name} are told the system and find the code that answers their question. Add ${organizationName}'s repositories, or a GitHub repository, then connect them.`
              : `${base.name} has no applications yet. Those who manage ${organizationName}'s knowledge bases add them.`
          }
          actions={
            canManage ? (
              <AddMenu
                trigger={<Button variant="outline" />}
                organizationName={organizationName}
                onPick={setAdding}
              >
                Add an application
                <Icon icon="down" data-icon="inline-end" />
              </AddMenu>
            ) : undefined
          }
          steps={[
            { icon: CODE_REPOSITORIES_ICON, label: "Add applications" },
            { icon: "link", label: "Connect them on the map" },
            { icon: "robot", label: "Attach it to agents" },
          ]}
        />
      ) : !tabled ? (
        list.isPending ? (
          <Skeleton className="m-4 h-[30rem] rounded-(--radius-card)" />
        ) : (
          <SystemMap
            scope={scope}
            baseName={base.name}
            applications={repositories}
            canManage={canManage}
            onOpen={openAt}
          />
        )
      ) : (
        <ActionsContext.Provider value={actions}>
          <DataTable
            className="rounded-none border-0"
            title={base.name}
            description={
              base.description ||
              `The applications in ${base.name}, whose code ${organizationName}'s agents search: ${repositories.length} ${repositories.length === 1 ? "application" : "applications"}, ${repositories.filter((r) => r.last_success).length} ingested into the code graph.`
            }
            columns={COLUMNS}
            data={repositories}
            isLoading={list.isPending}
            getRowId={(repository) => repository.id}
            features={FEATURES}
            initialState={INITIAL_STATE}
            stateKey="system-knowledge-applications"
            exportFileName="knowledge-base-applications"
            labels={{ rows: "applications", rowSingular: "application" }}
            onRowClick={(row) => openRepository(row.original)}
          />
        </ActionsContext.Provider>
      )}

      <IncludeRepositoryDialog
        open={adding === "existing"}
        onOpenChange={(next) => !next && setAdding(undefined)}
        baseName={base.name}
        organizationName={organizationName}
        repositories={candidates}
        onAddNew={() => setAdding("new")}
        onSubmit={async (repositoryId) => {
          const added = await include.mutateAsync({
            repository_id: repositoryId,
          })
          openRepository(added)
        }}
      />
      <AddRepositoryDialog
        open={adding === "new"}
        onOpenChange={(next) => !next && setAdding(undefined)}
        organizationName={organizationName}
        description={`A GitHub repository for ${base.name}: ${organizationName} gets it too (under Knowledge bases → Code repositories) when it doesn't have it, and ingests it into the code graph, so agents searching ${base.name} search its code.`}
        onSubmit={async (input) => {
          const added = await include.mutateAsync(input)
          openRepository(added)
        }}
      />
      <DeleteDialog
        open={removing !== undefined}
        onClose={() => setRemoving(undefined)}
        title={`Remove ${removing ? fullName(removing) : "the repository"} from ${base.name}?`}
        description={`Its connections on ${base.name}'s system map go with it, and agents searching ${base.name} stop finding its code. It stays in ${organizationName}, with its code graph, and in any other knowledge base that has it.`}
        confirmLabel="Remove application"
        onConfirm={async () => {
          await remove.mutateAsync(removing!.id)
          if (open?.id === removing?.id) openRepository(undefined)
        }}
      />
      <KnowledgeSearchDialog
        open={testing}
        onOpenChange={setTesting}
        scope={scope}
        baseName={base.name}
        code
        onOpenHit={(hit) =>
          onSearch({
            repository: hit.repository_id ?? undefined,
            section: undefined,
            node: hit.node_id ?? undefined,
          })
        }
      />
    </>
  )
}

/** The menu a repository is added from: one of the organization's, or a GitHub URL. */
function AddMenu({
  trigger,
  children,
  organizationName,
  onPick,
}: {
  trigger: React.ReactElement
  children: React.ReactNode
  organizationName: string
  onPick: (from: "existing" | "new") => void
}) {
  return (
    <DropdownMenu>
      <DropdownMenuTrigger render={trigger}>{children}</DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-80">
        <DropdownMenuGroup>
          <DropdownMenuLabel>Add an application…</DropdownMenuLabel>
          <DropdownMenuItem
            onClick={() => onPick("existing")}
            className="items-start"
          >
            <Icon icon={CODE_REPOSITORIES_ICON} className="mt-0.5" />
            <span className="flex min-w-0 flex-col">
              <span className="text-foreground">
                From {organizationName}'s repositories
              </span>
              <span className="text-2xs text-muted-foreground">
                One under Knowledge bases → Code repositories
              </span>
            </span>
          </DropdownMenuItem>
          <DropdownMenuItem
            onClick={() => onPick("new")}
            className="items-start"
          >
            <Icon icon={GithubIcon} className="mt-0.5" />
            <span className="flex min-w-0 flex-col">
              <span className="text-foreground">A GitHub repository</span>
              <span className="text-2xs text-muted-foreground">
                By its URL, added to {organizationName} and ingested
              </span>
            </span>
          </DropdownMenuItem>
        </DropdownMenuGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
