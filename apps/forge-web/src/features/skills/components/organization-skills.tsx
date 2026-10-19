import * as React from "react"
import {
  FolderUploadIcon,
  GithubIcon,
  PencilEdit02Icon,
} from "@hugeicons/core-free-icons"

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
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { Spinner } from "@/components/ui/spinner"
import { DeleteDialog } from "@/features/admin/components/delete-dialog"
import {
  ADMIN_TABLE_FEATURES,
  PIN_ACTIONS,
} from "@/features/admin/components/table-config"
import { RowMenu } from "@/features/admin/components/table-parts"
import {
  organizationSkills,
  useCreatorJobs,
  useDismissJob,
  useOrganizationSkills,
  type CreatorJob,
  type SkillBundle,
  type SkillRecord,
} from "@/features/skills/lib/api"
import {
  SKILLS_ICON,
  formatBytes,
  sourceLabel,
} from "@/features/skills/lib/skill-form"
import { formatRelative } from "@/lib/format"
import { useScopeAccess } from "@/lib/hierarchy"
import { ImportSkillsDialog, type ImportFrom } from "./import-skills-dialog"
import { SkillCreatorDialog } from "./skill-creator-dialog"
import { SkillDialog } from "./skill-dialog"

const FEATURES: Partial<DataTableFeatureConfig> = {
  ...ADMIN_TABLE_FEATURES,
  pagination: false,
  faceting: false,
}

const INITIAL_STATE: InitialTableState = { columnPinning: PIN_ACTIONS }

// The dialogs show failures themselves.
const SILENT = { meta: { silent: true } }

type RowActions = {
  open: (skill: SkillRecord) => void
  remove?: (skill: SkillRecord) => void
}
const ActionsContext = React.createContext<RowActions | undefined>(undefined)

const helper = createColumnHelper<SkillRecord>()
const COLUMNS = helper.columns([
  helper.accessor("name", {
    header: "Skill",
    size: 360,
    enableHiding: false,
    meta: { label: "Skill" },
    cell: ({ row: { original: skill } }) => (
      <span className="flex min-w-0 flex-col">
        <span className="truncate font-mono text-xs font-medium text-foreground">
          {skill.name}
        </span>
        <span className="truncate text-2xs text-muted-foreground">
          {skill.description}
        </span>
      </span>
    ),
  }),
  helper.accessor((skill) => sourceLabel(skill.source), {
    id: "source",
    header: "From",
    size: 220,
    meta: { label: "From", cellClassName: "text-muted-foreground" },
    cell: ({ getValue }) => <span className="truncate">{getValue()}</span>,
  }),
  helper.accessor("file_count", {
    header: "Files",
    size: 90,
    meta: {
      label: "Files",
      cellClassName: "text-muted-foreground tabular-nums",
    },
  }),
  helper.accessor("size_bytes", {
    header: "Size",
    size: 100,
    meta: {
      label: "Size",
      cellClassName: "text-muted-foreground tabular-nums",
    },
    cell: ({ getValue }) => formatBytes(getValue()),
  }),
  helper.accessor("updated_at", {
    header: "Changed",
    size: 140,
    meta: { label: "Changed", cellClassName: "text-muted-foreground" },
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
    cell: ({ row }) => <SkillMenu skill={row.original} />,
  }),
])

function SkillMenu({ skill }: { skill: SkillRecord }) {
  const actions = React.useContext(ActionsContext)
  if (!actions) return null
  return (
    <RowMenu
      label={skill.name}
      onEdit={() => actions.open(skill)}
      editLabel={actions.remove ? "Edit…" : "View…"}
      editIcon={SKILLS_ICON}
      onDelete={actions.remove && (() => actions.remove?.(skill))}
    />
  )
}

/**
 * An organization's skills vault: skills (a SKILL.md and the files beside
 * it) its agents load when a task calls for one, given to an agent with a
 * Skills tool on the Agents canvas or a workflow's LLM agent. Skills are
 * written here, uploaded, imported from GitHub, or written by the AI skill
 * creator in the background; each opens in the builders' settings dialog.
 */
export function OrganizationSkills({
  organizationId,
  organizationName,
  openSkill,
  onOpenSkill,
}: {
  organizationId: string
  organizationName: string
  /** The skill open in its dialog, by ID (from the URL). */
  openSkill?: string
  onOpenSkill: (id: string | undefined) => void
}) {
  const list = useOrganizationSkills(organizationId)
  const jobs = useCreatorJobs(organizationId)
  const can = useScopeAccess(`org:${organizationId}`)
  const canManage = can("skills:manage")
  const scoped = organizationSkills.scope({ organizationId })
  const create = scoped.useCreate(SILENT)
  const update = scoped.useUpdate(SILENT)
  const remove = scoped.useDelete(SILENT)
  const [writing, setWriting] = React.useState(false)
  const [importing, setImporting] = React.useState<ImportFrom>()
  const [creating, setCreating] = React.useState(false)
  const [revising, setRevising] = React.useState<SkillRecord>()
  const [removing, setRemoving] = React.useState<SkillRecord>()
  const skills = React.useMemo(() => list.data ?? [], [list.data])
  const open = skills.find((s) => s.id === openSkill)

  const actions = React.useMemo<RowActions>(
    () => ({
      open: (skill) => onOpenSkill(skill.id),
      ...(canManage ? { remove: setRemoving } : {}),
    }),
    [canManage, onOpenSkill]
  )

  const added = async (skill: SkillBundle) => {
    const made = await create.mutateAsync(skill)
    setWriting(false)
    onOpenSkill(made.id)
    return made
  }
  const saved = async (id: string, skill: SkillBundle) => {
    await update.mutateAsync({ id, data: skill })
  }

  const startCreator = (skill?: SkillRecord) => {
    setRevising(skill)
    setCreating(true)
  }

  return (
    <>
      {canManage && (
        <ShellHeaderActions>
          <Button variant="outline" onClick={() => startCreator()}>
            <Icon icon="sparkles" data-icon="inline-start" />
            AI skill creator
          </Button>
          <AddSkillMenu
            onWrite={() => setWriting(true)}
            onImport={(from) => setImporting(from)}
          />
        </ShellHeaderActions>
      )}

      <CreatorJobs
        organizationId={organizationId}
        jobs={jobs.data ?? []}
        skills={skills}
        canManage={canManage}
        onOpenSkill={onOpenSkill}
        onRetry={(job) => {
          setRevising(skills.find((s) => s.id === job.input.revises))
          setCreating(true)
        }}
      />

      {list.error ? (
        <ErrorCallout
          title="Couldn't load the organization's skills"
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
      ) : !list.isPending && skills.length === 0 ? (
        <EmptyWorkspace
          illustration={<EmptyIllustration name="waiting" />}
          title={`Give ${organizationName}'s agents skills`}
          description="A skill is a playbook an agent loads when a task calls for it: a SKILL.md saying what it's for and how to do it, and files beside it (references, templates, scripts). Write one, upload a folder of them, import them from GitHub, or describe one to the AI skill creator."
          actions={
            canManage ? (
              <div className="flex flex-wrap justify-center gap-2">
                <Button variant="outline" onClick={() => startCreator()}>
                  <Icon icon="sparkles" data-icon="inline-start" />
                  AI skill creator
                </Button>
                <Button
                  variant="outline"
                  onClick={() => setImporting("upload")}
                >
                  <Icon icon={FolderUploadIcon} data-icon="inline-start" />
                  Upload skills
                </Button>
                <Button
                  variant="outline"
                  onClick={() => setImporting("github")}
                >
                  <Icon icon={GithubIcon} data-icon="inline-start" />
                  From GitHub
                </Button>
                <Button variant="outline" onClick={() => setWriting(true)}>
                  <Icon icon={PencilEdit02Icon} data-icon="inline-start" />
                  Write one
                </Button>
              </div>
            ) : undefined
          }
          steps={[
            { icon: SKILLS_ICON, label: "Add skills to the vault" },
            { icon: "robot", label: "Give an agent a Skills tool" },
            { icon: "check", label: "It loads one when a task matches" },
          ]}
        />
      ) : (
        <ActionsContext.Provider value={actions}>
          <DataTable
            className="rounded-none border-0"
            title="Skills vault"
            description={`The skills ${organizationName}'s agents can be given: each a playbook an agent loads when a task matches its description.`}
            columns={COLUMNS}
            data={skills}
            isLoading={list.isPending}
            getRowId={(skill) => skill.id}
            features={FEATURES}
            initialState={INITIAL_STATE}
            stateKey="organization-skills"
            exportFileName="skills"
            labels={{ rows: "skills", rowSingular: "skill" }}
            onRowClick={(row) => onOpenSkill(row.original.id)}
          />
        </ActionsContext.Provider>
      )}

      <SkillDialog
        open={writing || open !== undefined}
        onOpenChange={(next) => {
          if (next) return
          setWriting(false)
          onOpenSkill(undefined)
        }}
        organizationId={organizationId}
        skillId={writing ? undefined : open?.id}
        readOnly={!canManage}
        onCreate={added}
        onUpdate={saved}
        onDelete={canManage ? setRemoving : undefined}
        onRework={canManage ? (skill) => startCreator(skill) : undefined}
      />

      <ImportSkillsDialog
        open={importing !== undefined}
        onOpenChange={(next) => !next && setImporting(undefined)}
        organizationId={organizationId}
        from={importing ?? "upload"}
      />

      <SkillCreatorDialog
        open={creating}
        onOpenChange={(next) => {
          setCreating(next)
          if (!next) setRevising(undefined)
        }}
        organizationId={organizationId}
        revising={revising}
      />

      <DeleteDialog
        open={removing !== undefined}
        onClose={() => setRemoving(undefined)}
        title={`Delete ${removing?.name ?? "skill"}?`}
        description="Agents and workflows that have it fail to start until it's taken off their Skills tools. It can't be undone; download it first to keep a copy."
        confirmLabel="Delete skill"
        onConfirm={async () => {
          await remove.mutateAsync(removing!.id)
          if (openSkill === removing?.id) onOpenSkill(undefined)
        }}
      />
    </>
  )
}

function AddSkillMenu({
  onWrite,
  onImport,
}: {
  onWrite: () => void
  onImport: (from: ImportFrom) => void
}) {
  const items = [
    {
      key: "write",
      icon: PencilEdit02Icon,
      label: "Write a skill",
      summary: "Its SKILL.md and files, here",
      pick: onWrite,
    },
    {
      key: "upload",
      icon: FolderUploadIcon,
      label: "Upload skills…",
      summary: "A folder of skills, one skill, a SKILL.md or a zip",
      pick: () => onImport("upload"),
    },
    {
      key: "github",
      icon: GithubIcon,
      label: "Import from GitHub…",
      summary: "A repository's folder of skills, at a branch or tag",
      pick: () => onImport("github"),
    },
  ]
  return (
    <DropdownMenu>
      <DropdownMenuTrigger render={<PrimaryAction />}>
        Skill
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-80">
        <DropdownMenuGroup>
          <DropdownMenuLabel>Add to the vault</DropdownMenuLabel>
          {items.map((item) => (
            <DropdownMenuItem
              key={item.key}
              onClick={item.pick}
              className="items-start"
            >
              <Icon icon={item.icon} className="mt-0.5" />
              <span className="flex min-w-0 flex-col">
                <span className="text-foreground">{item.label}</span>
                <span className="text-2xs text-muted-foreground">
                  {item.summary}
                </span>
              </span>
            </DropdownMenuItem>
          ))}
        </DropdownMenuGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

/**
 * What the skill creator is writing, and what it wrote or couldn't: above
 * the vault until dismissed (a week at most).
 */
function CreatorJobs({
  organizationId,
  jobs,
  skills,
  canManage,
  onOpenSkill,
  onRetry,
}: {
  organizationId: string
  jobs: CreatorJob[]
  skills: SkillRecord[]
  canManage: boolean
  onOpenSkill: (id: string) => void
  onRetry: (job: CreatorJob) => void
}) {
  const dismiss = useDismissJob(organizationId)
  if (!jobs.length) return null
  return (
    <section
      aria-label="The skill creator's work"
      className="flex flex-col gap-1.5 border-b px-4 py-3"
    >
      {jobs.map((job) => {
        const made = skills.find((s) => s.id === job.result?.skill_id)
        return (
          <div
            key={job.id}
            className="flex items-start gap-3 rounded-(--radius-item) border bg-card px-3 py-2"
          >
            <span className="mt-0.5 shrink-0">
              {job.status === "running" ? (
                <Spinner />
              ) : job.status === "done" ? (
                <Icon
                  icon="completed"
                  size={16}
                  className="text-signal-success"
                />
              ) : (
                <Icon icon="failed" size={16} className="text-signal-failed" />
              )}
            </span>
            <div className="flex min-w-0 flex-1 flex-col gap-0.5">
              <span className="flex flex-wrap items-center gap-2 text-xs text-foreground">
                {job.status === "running"
                  ? job.input.revises
                    ? "The skill creator is reworking a skill…"
                    : "The skill creator is writing a skill…"
                  : job.status === "done"
                    ? made
                      ? `Wrote ${made.name}`
                      : "Wrote a skill (deleted since)"
                    : "The skill creator couldn't write it"}
                <Chip tone="neutral">{formatRelative(job.created_at)}</Chip>
              </span>
              <span className="line-clamp-2 text-2xs/[1.5] text-muted-foreground">
                {job.input.brief}
              </span>
              {job.status === "failed" && job.error && (
                <span className="text-2xs text-destructive">{job.error}</span>
              )}
              {job.status === "done" && job.notes.length > 0 && (
                <ul className="mt-1 list-disc pl-4 text-2xs/[1.5] text-muted-foreground">
                  {job.notes.map((note) => (
                    <li key={note}>{note}</li>
                  ))}
                </ul>
              )}
            </div>
            <div className="flex shrink-0 items-center gap-1">
              {job.status === "done" && made && (
                <Button
                  variant="outline"
                  size="xs"
                  onClick={() => onOpenSkill(made.id)}
                >
                  Open
                </Button>
              )}
              {job.status === "failed" && canManage && (
                <Button
                  variant="outline"
                  size="xs"
                  onClick={() => onRetry(job)}
                >
                  Try again
                </Button>
              )}
              {job.status !== "running" && canManage && (
                <Button
                  variant="ghost"
                  size="icon-xs"
                  aria-label="Dismiss"
                  onClick={() => dismiss.mutate(job.id)}
                >
                  <Icon icon="close" />
                </Button>
              )}
            </div>
          </div>
        )
      })}
    </section>
  )
}
