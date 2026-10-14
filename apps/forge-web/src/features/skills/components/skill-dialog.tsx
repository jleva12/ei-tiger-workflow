import * as React from "react"
import { Delete02Icon, Upload04Icon } from "@hugeicons/core-free-icons"
import { cn } from "cn"

import { ErrorCallout } from "@/components/forge/feedback"
import { Icon } from "@/components/forge/icon"
import { Chip } from "@/components/forge/status"
import { Button } from "@/components/ui/button"
import {
  Field,
  FieldDescription,
  FieldError,
  FieldLabel,
} from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { Skeleton } from "@/components/ui/skeleton"
import { Spinner } from "@/components/ui/spinner"
import { Textarea } from "@/components/ui/textarea"
import { toast } from "@/components/ui/toast"
import { toApiError, type ApiError } from "@/lib/api/index"
import { formatRelative } from "@/lib/format"
import { useOpenCount } from "@/features/admin/components/dialog-state"
import { downloadBlob } from "@/features/builder/components/utils"
import {
  SettingsBody,
  SettingsDialog,
  SettingsFooter,
  SettingsGlyph,
  SettingsHeader,
  SettingsHint,
  SettingsSection,
} from "@/features/builder/components/settings-dialog"
import {
  downloadSkill,
  usePullSkill,
  useSkill,
  type SkillBundle,
  type SkillFile,
  type SkillRecord,
} from "@/features/skills/lib/api"
import {
  MAX_DESCRIPTION,
  SKILLS_ICON,
  emptySkill,
  fileBytes,
  formatBytes,
  hasProblems,
  skillFileOf,
  skillProblems,
  slugOf,
  sourceLabel,
} from "@/features/skills/lib/skill-form"

type SkillDialogProps = {
  open: boolean
  onOpenChange: (open: boolean) => void
  organizationId: string
  /** The skill to show and edit, by ID; without one the dialog writes a new one. */
  skillId?: string
  /** Show it without letting it be changed (no `skills:manage`). */
  readOnly?: boolean
  /** Adds a new skill; rejects with the API's error, which it shows. */
  onCreate: (skill: SkillBundle) => Promise<SkillRecord>
  /** Saves the skill; rejects with the API's error, which it shows. */
  onUpdate: (id: string, skill: SkillBundle) => Promise<unknown>
  onDelete?: (skill: SkillRecord) => void
  /** Has the skill creator rework it. */
  onRework?: (skill: SkillRecord) => void
}

/**
 * Writes a new skill, or shows and edits one of the vault's, in the
 * builders' settings dialog: its SKILL.md (name, description, the
 * instructions the agent follows) and the files beside it. One imported
 * from GitHub can be pulled again; any can be downloaded as a zip of its
 * folder, as other tools that read skills take one.
 */
export function SkillDialog({
  open,
  onOpenChange,
  skillId,
  ...props
}: SkillDialogProps) {
  const count = useOpenCount(open)
  // While it closes it keeps showing what it showed: a skill, or (null) a new one.
  const [shown, setShown] = React.useState<string | null | undefined>(
    open ? (skillId ?? null) : undefined
  )
  const target = open ? (skillId ?? null) : undefined
  if (target !== undefined && target !== shown) setShown(target)
  return (
    <SettingsDialog open={open} onOpenChange={onOpenChange}>
      {shown !== undefined && (
        <SkillDialogBody
          key={`${count}:${shown ?? "new"}`}
          skillId={shown ?? undefined}
          onDone={() => onOpenChange(false)}
          {...props}
        />
      )}
    </SettingsDialog>
  )
}

function SkillDialogBody({
  organizationId,
  skillId,
  onDone,
  ...props
}: Omit<SkillDialogProps, "open" | "onOpenChange" | "skillId"> & {
  skillId?: string
  onDone: () => void
}) {
  const record = useSkill(organizationId, skillId)
  if (skillId && !record.data?.skill) {
    return (
      <>
        <SettingsHeader
          glyph={<SettingsGlyph icon={SKILLS_ICON} tone="action" />}
          title={record.data?.name ?? "Skill"}
          closeLabel="Close the skill"
        />
        <SettingsBody>
          <SettingsSection>
            {record.error ? (
              <ErrorCallout
                title="Couldn't load the skill"
                action={
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => void record.refetch()}
                  >
                    Retry
                  </Button>
                }
              >
                {record.error.message}
              </ErrorCallout>
            ) : (
              <div className="flex flex-col gap-3">
                <Skeleton className="h-8 w-1/2" />
                <Skeleton className="h-16" />
                <Skeleton className="h-64" />
              </div>
            )}
          </SettingsSection>
        </SettingsBody>
      </>
    )
  }
  return (
    <SkillForm
      organizationId={organizationId}
      record={skillId ? record.data : undefined}
      onDone={onDone}
      {...props}
    />
  )
}

function SkillForm({
  organizationId,
  record,
  readOnly = false,
  onCreate,
  onUpdate,
  onDelete,
  onRework,
  onDone,
}: Omit<SkillDialogProps, "open" | "onOpenChange" | "skillId"> & {
  record?: SkillRecord
  onDone: () => void
}) {
  const saved = record?.skill
  const [skill, setSkill] = React.useState<SkillBundle>(
    () => saved ?? emptySkill()
  )
  const [submitted, setSubmitted] = React.useState(false)
  const [pending, setPending] = React.useState(false)
  const [error, setError] = React.useState<ApiError>()
  const [openFile, setOpenFile] = React.useState<number>()
  const pull = usePullSkill(organizationId)
  const upload = React.useRef<HTMLInputElement>(null)
  const ids = {
    name: React.useId(),
    description: React.useId(),
    instructions: React.useId(),
  }
  // Pulled from GitHub: the form starts again from what came.
  const [base, setBase] = React.useState(saved)
  if (saved && saved !== base) {
    setBase(saved)
    setSkill(saved)
  }

  const problems = skillProblems(skill)
  const shownProblems = submitted ? problems : {}
  const dirty = JSON.stringify(skill) !== JSON.stringify(base ?? emptySkill())
  const set = <K extends keyof SkillBundle>(key: K, value: SkillBundle[K]) =>
    setSkill((current) => ({ ...current, [key]: value }))
  const setFile = (index: number, file: Partial<SkillFile>) =>
    set(
      "files",
      skill.files.map((f, i) => (i === index ? { ...f, ...file } : f))
    )

  async function submit(event: React.SubmitEvent<HTMLFormElement>) {
    event.preventDefault()
    setSubmitted(true)
    if (hasProblems(problems)) return
    setPending(true)
    setError(undefined)
    const clean = {
      ...skill,
      files: skill.files.map((f) => ({ ...f, path: f.path.trim() })),
    }
    try {
      if (record) {
        await onUpdate(record.id, clean)
        setBase(clean)
        toast.add({ title: `Saved ${clean.name}`, type: "success" })
      } else {
        await onCreate(clean)
      }
    } catch (caught) {
      setError(toApiError(caught))
    } finally {
      setPending(false)
    }
  }

  async function addFiles(files: FileList | null) {
    if (!files?.length) return
    const added = await Promise.all(
      Array.from(files).map((file) =>
        skillFileOf(
          file,
          file.name.endsWith(".md")
            ? `references/${file.name}`
            : `assets/${file.name}`
        )
      )
    )
    set("files", [...skill.files, ...added])
  }

  async function download() {
    if (!record) return
    try {
      const blob = await downloadSkill(organizationId, record.id)
      downloadBlob(blob, `${record.name}.zip`)
    } catch (caught) {
      toast.add({
        title: "Couldn't download the skill",
        description: toApiError(caught).message,
        type: "error",
      })
    }
  }

  const source = record?.source
  return (
    <>
      <SettingsHeader
        glyph={<SettingsGlyph icon={SKILLS_ICON} tone="action" />}
        title={record ? record.name : "New skill"}
        description={
          record
            ? `${sourceLabel(record.source)} · changed ${formatRelative(record.updated_at)}`
            : "Instructions and files an agent loads when a task calls for them"
        }
        closeLabel="Close the skill"
      />
      <form
        onSubmit={submit}
        noValidate
        className="flex min-h-0 flex-1 flex-col"
      >
        <SettingsBody>
          <SettingsSection className="gap-3">
            <p className="text-xs/[1.6] text-muted-foreground">
              Agents see every skill they&apos;re given by its name and
              description, and load its instructions only when a task matches
              the description: give one to an agent with a Skills tool, on the
              Agents canvas or a workflow&apos;s LLM agent.
            </p>
            {error && (
              <ErrorCallout
                title={`Couldn't ${record ? "save" : "add"} the skill`}
              >
                {error.message}
              </ErrorCallout>
            )}
            {source?.kind === "github" && (
              <div className="flex flex-wrap items-center gap-2 rounded-(--radius-item) border bg-muted/40 px-3 py-2 text-2xs text-muted-foreground">
                <Icon icon="branch" size={14} />
                <span className="min-w-0 flex-1 truncate">
                  From{" "}
                  <a
                    className="text-foreground underline-offset-2 hover:underline"
                    href={`${source.repository}/tree/${source.commit}/${source.path}`}
                    target="_blank"
                    rel="noreferrer"
                  >
                    {source.repository.replace("https://github.com/", "")}
                    {source.path ? `/${source.path}` : ""}
                  </a>{" "}
                  at {source.ref} ({source.commit.slice(0, 7)})
                </span>
                {!readOnly && (
                  <Button
                    type="button"
                    variant="outline"
                    size="xs"
                    disabled={pull.isPending}
                    onClick={() =>
                      pull.mutate(record!.id, {
                        onSuccess: (pulled) =>
                          toast.add({
                            title: `Pulled ${pulled.name} again`,
                            description: dirty
                              ? "Your unsaved changes were replaced."
                              : undefined,
                            type: "success",
                          }),
                        onError: (caught) => setError(toApiError(caught)),
                      })
                    }
                  >
                    {pull.isPending ? (
                      <Spinner data-icon="inline-start" />
                    ) : (
                      <Icon icon="refresh" data-icon="inline-start" />
                    )}
                    Pull again
                  </Button>
                )}
              </div>
            )}
          </SettingsSection>

          <SettingsSection title="SKILL.md">
            <Field data-invalid={Boolean(shownProblems.name) || undefined}>
              <FieldLabel htmlFor={ids.name}>Name</FieldLabel>
              <Input
                id={ids.name}
                className="font-mono text-xs"
                value={skill.name}
                readOnly={readOnly}
                autoFocus={!record}
                placeholder="triage-support-tickets"
                aria-invalid={Boolean(shownProblems.name)}
                onChange={(e) => set("name", e.target.value)}
                onBlur={() => !readOnly && set("name", slugOf(skill.name))}
              />
              {shownProblems.name ? (
                <FieldError>{shownProblems.name}</FieldError>
              ) : (
                <FieldDescription>
                  Lowercase words joined by hyphens: what the agent loads it by.
                </FieldDescription>
              )}
            </Field>
            <Field
              data-invalid={Boolean(shownProblems.description) || undefined}
            >
              <FieldLabel htmlFor={ids.description}>
                What it does and when to use it
              </FieldLabel>
              <Textarea
                id={ids.description}
                rows={3}
                value={skill.description}
                readOnly={readOnly}
                placeholder="Triages incoming support tickets by product area and urgency. Use when a new ticket arrives or someone asks to sort the queue."
                aria-invalid={Boolean(shownProblems.description)}
                onChange={(e) => set("description", e.target.value)}
              />
              {shownProblems.description ? (
                <FieldError>{shownProblems.description}</FieldError>
              ) : (
                <FieldDescription>
                  The agent decides when to load the skill from this alone: use
                  the words a request would. {skill.description.length}/
                  {MAX_DESCRIPTION}
                </FieldDescription>
              )}
            </Field>
            <Field
              data-invalid={Boolean(shownProblems.instructions) || undefined}
            >
              <FieldLabel htmlFor={ids.instructions}>Instructions</FieldLabel>
              <Textarea
                id={ids.instructions}
                rows={16}
                className="max-h-[26rem] overflow-y-auto font-mono text-xs/[1.6]"
                value={skill.instructions}
                readOnly={readOnly}
                aria-invalid={Boolean(shownProblems.instructions)}
                onChange={(e) => set("instructions", e.target.value)}
              />
              {shownProblems.instructions ? (
                <FieldError>{shownProblems.instructions}</FieldError>
              ) : (
                <FieldDescription>
                  Markdown: the steps the agent follows, what it checks and what
                  it hands back. Point to a file by its path (references/…) for
                  what it reads only when it needs it.
                </FieldDescription>
              )}
            </Field>
          </SettingsSection>

          <SettingsSection
            title={`Files${skill.files.length ? ` (${skill.files.length})` : ""}`}
          >
            {skill.files.length === 0 ? (
              <p className="text-xs text-muted-foreground">
                None. Long reference material (a field list, a policy, a
                template) reads best as a file the instructions point to.
              </p>
            ) : (
              <ul className="flex flex-col gap-1.5">
                {skill.files.map((file, index) => {
                  const problem = shownProblems.paths?.[index]
                  const editing = openFile === index
                  return (
                    <li
                      key={index}
                      className={cn(
                        "flex flex-col gap-2 rounded-(--radius-item) border px-2.5 py-2",
                        problem && "border-destructive/60"
                      )}
                    >
                      <div className="flex items-center gap-2">
                        <Input
                          aria-label="The file's path"
                          className="h-7 flex-1 font-mono text-xs"
                          value={file.path}
                          readOnly={readOnly}
                          aria-invalid={Boolean(problem)}
                          onChange={(e) =>
                            setFile(index, { path: e.target.value })
                          }
                        />
                        <span className="shrink-0 text-2xs text-muted-foreground tabular-nums">
                          {formatBytes(fileBytes(file))}
                        </span>
                        {file.encoding === "base64" ? (
                          <Chip tone="neutral">Binary</Chip>
                        ) : (
                          <Button
                            type="button"
                            variant="ghost"
                            size="xs"
                            onClick={() =>
                              setOpenFile(editing ? undefined : index)
                            }
                          >
                            {editing ? "Hide" : readOnly ? "View" : "Edit"}
                          </Button>
                        )}
                        {!readOnly && (
                          <Button
                            type="button"
                            variant="ghost"
                            size="icon-xs"
                            aria-label={`Remove ${file.path}`}
                            onClick={() => {
                              setOpenFile(undefined)
                              set(
                                "files",
                                skill.files.filter((_, i) => i !== index)
                              )
                            }}
                          >
                            <Icon icon={Delete02Icon} />
                          </Button>
                        )}
                      </div>
                      {problem && (
                        <span className="text-2xs text-destructive">
                          {problem}
                        </span>
                      )}
                      {editing && file.encoding === "utf-8" && (
                        <Textarea
                          aria-label={`${file.path}'s content`}
                          rows={12}
                          className="max-h-[26rem] overflow-y-auto font-mono text-xs/[1.6]"
                          value={file.content}
                          readOnly={readOnly}
                          onChange={(e) =>
                            setFile(index, { content: e.target.value })
                          }
                        />
                      )}
                    </li>
                  )
                })}
              </ul>
            )}
            {shownProblems.files && (
              <span className="text-2xs text-destructive">
                {shownProblems.files}
              </span>
            )}
            {!readOnly && (
              <div className="flex flex-wrap gap-2">
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  onClick={() => {
                    set("files", [
                      ...skill.files,
                      { path: "references/", content: "", encoding: "utf-8" },
                    ])
                    setOpenFile(skill.files.length)
                  }}
                >
                  <Icon icon="plus" data-icon="inline-start" />
                  New file
                </Button>
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  onClick={() => upload.current?.click()}
                >
                  <Icon icon={Upload04Icon} data-icon="inline-start" />
                  Add files…
                </Button>
                <input
                  ref={upload}
                  type="file"
                  multiple
                  hidden
                  onChange={(e) => {
                    void addFiles(e.target.files)
                    e.target.value = ""
                  }}
                />
              </div>
            )}
          </SettingsSection>
        </SettingsBody>
        <SettingsFooter>
          {record && !readOnly && onDelete && (
            <Button
              type="button"
              variant="destructive"
              size="sm"
              onClick={() => onDelete(record)}
            >
              Delete
            </Button>
          )}
          {record && (
            <Button type="button" variant="ghost" size="sm" onClick={download}>
              <Icon icon="download" data-icon="inline-start" />
              Download
            </Button>
          )}
          {record && !readOnly && onRework && (
            <Button
              type="button"
              variant="ghost"
              size="sm"
              onClick={() => onRework(record)}
            >
              <Icon icon="sparkles" data-icon="inline-start" />
              Rework with AI
            </Button>
          )}
          <SettingsHint>
            {readOnly
              ? "You can't change the organization's skills"
              : dirty
                ? "Unsaved changes"
                : record
                  ? "Saved"
                  : "Added to the vault"}
          </SettingsHint>
          {readOnly ? (
            <Button
              type="button"
              size="sm"
              className="max-[600px]:ml-auto"
              onClick={onDone}
            >
              Done
            </Button>
          ) : (
            <>
              <Button
                type="button"
                variant="outline"
                size="sm"
                className="max-[600px]:ml-auto"
                onClick={onDone}
              >
                {record && !dirty ? "Close" : "Cancel"}
              </Button>
              <Button
                type="submit"
                size="sm"
                disabled={pending || (record !== undefined && !dirty)}
              >
                {pending && <Spinner data-icon="inline-start" />}
                {record ? "Save" : "Add skill"}
              </Button>
            </>
          )}
        </SettingsFooter>
      </form>
    </>
  )
}
