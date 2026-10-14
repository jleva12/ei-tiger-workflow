import * as React from "react"
import {
  FileUploadIcon,
  FolderUploadIcon,
  GithubIcon,
} from "@hugeicons/core-free-icons"
import { cn } from "cn"

import { ErrorCallout } from "@/components/forge/feedback"
import { Icon } from "@/components/forge/icon"
import { Chip } from "@/components/forge/status"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Field, FieldDescription, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Spinner } from "@/components/ui/spinner"
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { toast } from "@/components/ui/toast"
import { toApiError, type ApiError } from "@/lib/api/index"
import { useOpenCount } from "@/features/admin/components/dialog-state"
import {
  SettingsBody,
  SettingsDialog,
  SettingsFooter,
  SettingsGlyph,
  SettingsHeader,
  SettingsHint,
  SettingsSection,
} from "@/features/builder/components/settings-dialog"
import { useOrganizationCredentials } from "@/features/credentials/lib/api"
import { readDroppedEntries } from "@/features/knowledge/lib/knowledge"
import {
  readGitHub,
  readUpload,
  useImportSkills,
  type FoundSkill,
  type ReadResult,
} from "@/features/skills/lib/api"
import {
  SKILLS_ICON,
  formatBytes,
  skillBytes,
} from "@/features/skills/lib/skill-form"

export type ImportFrom = "upload" | "github"

const NO_CREDENTIAL = "__none__"

/**
 * Brings skills into the vault: uploaded (a folder of skills, one skill's
 * folder, a SKILL.md, or a zip, picked or dropped) or read from a GitHub
 * repository's folder. What's found is listed first, each skill with its
 * files and any it would replace; the ones ticked are added together.
 */
export function ImportSkillsDialog({
  open,
  onOpenChange,
  organizationId,
  from,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  organizationId: string
  /** Where it opens: an upload, or GitHub. */
  from: ImportFrom
}) {
  const count = useOpenCount(open)
  return (
    <SettingsDialog open={open} onOpenChange={onOpenChange}>
      <ImportBody
        key={count}
        organizationId={organizationId}
        initialFrom={from}
        onDone={() => onOpenChange(false)}
      />
    </SettingsDialog>
  )
}

/** A file picked or dropped, with its path in what was picked. */
type Picked = { file: File; path: string }

function ImportBody({
  organizationId,
  initialFrom,
  onDone,
}: {
  organizationId: string
  initialFrom: ImportFrom
  onDone: () => void
}) {
  const [from, setFrom] = React.useState<ImportFrom>(initialFrom)
  const [result, setResult] = React.useState<ReadResult>()
  const [chosen, setChosen] = React.useState<Set<number>>(new Set())
  const [reading, setReading] = React.useState(false)
  const [error, setError] = React.useState<ApiError>()
  const [what, setWhat] = React.useState("")
  const importSkills = useImportSkills(organizationId)

  function found(read: ReadResult, label: string) {
    setResult(read)
    setWhat(label)
    setChosen(new Set(read.skills.map((_, index) => index)))
  }

  async function read(work: () => Promise<ReadResult>, label: string) {
    setReading(true)
    setError(undefined)
    setResult(undefined)
    try {
      found(await work(), label)
    } catch (caught) {
      setError(toApiError(caught))
    } finally {
      setReading(false)
    }
  }

  async function add() {
    if (!result) return
    const picked = result.skills.filter((_, index) => chosen.has(index))
    try {
      const added = await importSkills.mutateAsync(
        picked.map((s) => ({
          skill: s.skill,
          source: s.source,
          replace: s.existing,
        }))
      )
      toast.add({
        title:
          added.length === 1
            ? `Added ${added[0].name} to the vault`
            : `Added ${added.length} skills to the vault`,
        type: "success",
      })
      onDone()
    } catch (caught) {
      setError(toApiError(caught))
    }
  }

  const picked = result?.skills.filter((_, index) => chosen.has(index)) ?? []
  return (
    <>
      <SettingsHeader
        glyph={<SettingsGlyph icon={SKILLS_ICON} tone="action" />}
        title="Import skills"
        description="Every folder with a SKILL.md is a skill: pick the ones to add"
        closeLabel="Close the import"
      />
      <SettingsBody>
        <SettingsSection className="gap-3">
          <Tabs
            value={from}
            onValueChange={(value) => {
              setFrom(String(value) as ImportFrom)
              setResult(undefined)
              setError(undefined)
            }}
          >
            <TabsList>
              <TabsTrigger value="upload">
                <Icon icon={FolderUploadIcon} data-icon="inline-start" />
                Upload
              </TabsTrigger>
              <TabsTrigger value="github">
                <Icon icon={GithubIcon} data-icon="inline-start" />
                GitHub
              </TabsTrigger>
            </TabsList>
          </Tabs>
          {from === "upload" ? (
            <UploadPicker
              disabled={reading}
              onPick={(files, label) =>
                void read(() => readUpload(organizationId, files), label)
              }
            />
          ) : (
            <GitHubPicker
              organizationId={organizationId}
              reading={reading}
              onRead={(body) =>
                void read(
                  () => readGitHub(organizationId, body),
                  body.url.replace(/^https:\/\/github\.com\//, "")
                )
              }
            />
          )}
          {error && (
            <ErrorCallout
              title={result ? "Couldn't add the skills" : "Couldn't read them"}
            >
              {error.message}
            </ErrorCallout>
          )}
        </SettingsSection>
        {reading && (
          <SettingsSection>
            <span className="flex items-center gap-2 text-xs text-muted-foreground">
              <Spinner /> Reading{" "}
              {from === "github" ? "the repository" : "the files"}…
            </span>
          </SettingsSection>
        )}
        {result && (
          <FoundList
            result={result}
            what={what}
            chosen={chosen}
            onToggle={(index, on) =>
              setChosen((current) => {
                const next = new Set(current)
                if (on) next.add(index)
                else next.delete(index)
                return next
              })
            }
          />
        )}
      </SettingsBody>
      <SettingsFooter>
        <SettingsHint>
          {result
            ? `${picked.length} of ${result.skills.length} picked`
            : "Nothing's added until you say so"}
        </SettingsHint>
        <Button
          type="button"
          variant="outline"
          size="sm"
          className="max-[600px]:ml-auto"
          onClick={onDone}
        >
          Cancel
        </Button>
        <Button
          type="button"
          size="sm"
          disabled={!picked.length || importSkills.isPending}
          onClick={() => void add()}
        >
          {importSkills.isPending && <Spinner data-icon="inline-start" />}
          {picked.length === 1
            ? "Add 1 skill"
            : picked.length
              ? `Add ${picked.length} skills`
              : "Add skills"}
        </Button>
      </SettingsFooter>
    </>
  )
}

function UploadPicker({
  disabled,
  onPick,
}: {
  disabled: boolean
  onPick: (files: Picked[], label: string) => void
}) {
  const folderInput = React.useRef<HTMLInputElement | null>(null)
  const fileInput = React.useRef<HTMLInputElement>(null)
  const [over, setOver] = React.useState(false)

  const label = (files: Picked[]) =>
    files.length === 1
      ? files[0].path
      : (files[0]?.path.split("/")[0] ?? "the files")

  async function dropped(event: React.DragEvent) {
    event.preventDefault()
    setOver(false)
    const entries = Array.from(event.dataTransfer.items)
      .map((item) => item.webkitGetAsEntry?.())
      .filter((entry): entry is FileSystemEntry => Boolean(entry))
    const items = entries.length
      ? await readDroppedEntries(entries)
      : Array.from(event.dataTransfer.files).map((file) => ({
          file,
          folders: [] as string[],
        }))
    const files = items.map(({ file, folders }) => ({
      file,
      path: [...folders, file.name].join("/"),
    }))
    if (files.length) onPick(files, label(files))
  }

  return (
    <div
      onDragOver={(event) => {
        event.preventDefault()
        setOver(true)
      }}
      onDragLeave={() => setOver(false)}
      onDrop={(event) => void dropped(event)}
      className={cn(
        "flex flex-col items-center gap-3 rounded-(--radius-card) border border-dashed px-4 py-6 text-center",
        over && "border-foreground/40 bg-muted/50"
      )}
    >
      <p className="max-w-md text-xs/[1.6] text-muted-foreground">
        Drop a folder of skills, one skill&apos;s folder, a SKILL.md or a zip (a
        .skill file too) here, or pick one.
      </p>
      <div className="flex flex-wrap justify-center gap-2">
        <Button
          type="button"
          variant="outline"
          size="sm"
          disabled={disabled}
          onClick={() => folderInput.current?.click()}
        >
          <Icon icon={FolderUploadIcon} data-icon="inline-start" />
          Choose a folder…
        </Button>
        <Button
          type="button"
          variant="outline"
          size="sm"
          disabled={disabled}
          onClick={() => fileInput.current?.click()}
        >
          <Icon icon={FileUploadIcon} data-icon="inline-start" />
          Choose a file…
        </Button>
      </div>
      <input
        ref={(input) => {
          folderInput.current = input
          if (input) input.webkitdirectory = true
        }}
        type="file"
        hidden
        onChange={(event) => {
          const files = Array.from(event.target.files ?? []).map((file) => ({
            file,
            path: file.webkitRelativePath || file.name,
          }))
          event.target.value = ""
          if (files.length) onPick(files, label(files))
        }}
      />
      <input
        ref={fileInput}
        type="file"
        hidden
        accept=".md,.zip,.skill"
        onChange={(event) => {
          const files = Array.from(event.target.files ?? []).map((file) => ({
            file,
            path: file.name,
          }))
          event.target.value = ""
          if (files.length) onPick(files, label(files))
        }}
      />
    </div>
  )
}

function GitHubPicker({
  organizationId,
  reading,
  onRead,
}: {
  organizationId: string
  reading: boolean
  onRead: (body: {
    url: string
    path: string
    ref: string
    credential: string
  }) => void
}) {
  const [url, setUrl] = React.useState("")
  const [path, setPath] = React.useState("")
  const [ref, setRef] = React.useState("")
  const [credential, setCredential] = React.useState(NO_CREDENTIAL)
  const credentials = useOrganizationCredentials(organizationId)
  const choices = [
    { value: NO_CREDENTIAL, label: "None: a public repository" },
    ...(credentials.data ?? []).map((c) => ({ value: c.id, label: c.name })),
  ]
  const ids = {
    url: React.useId(),
    path: React.useId(),
    ref: React.useId(),
    credential: React.useId(),
  }
  return (
    <form
      className="flex flex-col gap-3"
      onSubmit={(event) => {
        event.preventDefault()
        if (!url.trim()) return
        onRead({
          url: url.trim(),
          path: path.trim(),
          ref: ref.trim(),
          credential: credential === NO_CREDENTIAL ? "" : credential,
        })
      }}
    >
      <Field>
        <FieldLabel htmlFor={ids.url}>Repository</FieldLabel>
        <Input
          id={ids.url}
          autoFocus
          value={url}
          placeholder="https://github.com/anthropics/skills/tree/main/skills"
          onChange={(e) => setUrl(e.target.value)}
        />
        <FieldDescription>
          The repository&apos;s URL, a folder&apos;s as GitHub shows it, or
          owner/repository.
        </FieldDescription>
      </Field>
      <div className="grid grid-cols-2 gap-3 max-[600px]:grid-cols-1">
        <Field>
          <FieldLabel htmlFor={ids.path}>Path to the skills</FieldLabel>
          <Input
            id={ids.path}
            className="font-mono text-xs"
            value={path}
            placeholder="skills"
            onChange={(e) => setPath(e.target.value)}
          />
          <FieldDescription>
            Empty: the URL&apos;s folder, or the whole repository.
          </FieldDescription>
        </Field>
        <Field>
          <FieldLabel htmlFor={ids.ref}>Branch, tag or commit</FieldLabel>
          <Input
            id={ids.ref}
            className="font-mono text-xs"
            value={ref}
            placeholder="main"
            onChange={(e) => setRef(e.target.value)}
          />
          <FieldDescription>
            Empty: the URL&apos;s, or the default branch.
          </FieldDescription>
        </Field>
      </div>
      <Field>
        <FieldLabel htmlFor={ids.credential}>Signs in with</FieldLabel>
        <Select
          items={choices}
          value={credential}
          onValueChange={(value) => value && setCredential(String(value))}
        >
          <SelectTrigger id={ids.credential} className="w-full">
            <SelectValue />
          </SelectTrigger>
          <SelectContent alignItemWithTrigger={false}>
            {choices.map((choice) => (
              <SelectItem key={choice.value} value={choice.value}>
                {choice.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <FieldDescription>
          For a private repository: one of the organization&apos;s credentials
          with a GitHub token, sent to https://api.github.com.
        </FieldDescription>
      </Field>
      <div>
        <Button
          type="submit"
          size="sm"
          variant="outline"
          disabled={reading || !url.trim()}
        >
          {reading ? (
            <Spinner data-icon="inline-start" />
          ) : (
            <Icon icon={GithubIcon} data-icon="inline-start" />
          )}
          Find its skills
        </Button>
      </div>
    </form>
  )
}

function FoundList({
  result,
  what,
  chosen,
  onToggle,
}: {
  result: ReadResult
  what: string
  chosen: Set<number>
  onToggle: (index: number, on: boolean) => void
}) {
  const groupId = React.useId()
  return (
    <SettingsSection
      title={
        result.skills.length
          ? `${result.skills.length === 1 ? "1 skill" : `${result.skills.length} skills`} in ${what}`
          : `No skills in ${what}`
      }
    >
      {result.skills.length === 0 ? (
        <p className="text-xs/[1.6] text-muted-foreground">
          A skill is a folder with a SKILL.md: YAML frontmatter with its name
          and description, then its instructions.
        </p>
      ) : (
        <ul className="flex flex-col gap-1.5" aria-labelledby={groupId}>
          {result.skills.map((found, index) => (
            <FoundRow
              key={`${found.folder}:${found.skill.name}`}
              id={`${groupId}-${index}`}
              found={found}
              checked={chosen.has(index)}
              onChange={(on) => onToggle(index, on)}
            />
          ))}
        </ul>
      )}
      {result.problems.length > 0 && (
        <div className="flex flex-col gap-1 rounded-(--radius-item) bg-warning-surface px-3 py-2 text-2xs/[1.6] text-warning-foreground">
          <span className="font-medium">
            {result.problems.length === 1
              ? "1 SKILL.md couldn't be read"
              : `${result.problems.length} SKILL.md files couldn't be read`}
          </span>
          {result.problems.map((problem) => (
            <span key={problem.path}>
              <code className="font-mono">{problem.path}</code>:{" "}
              {problem.message}
            </span>
          ))}
        </div>
      )}
      {result.skipped.length > 0 && (
        <p className="text-2xs text-subtle">
          {result.skipped.length === 1
            ? "1 file isn't in any skill's folder and was left out."
            : `${result.skipped.length} files aren't in any skill's folder and were left out.`}
        </p>
      )}
    </SettingsSection>
  )
}

function FoundRow({
  id,
  found,
  checked,
  onChange,
}: {
  id: string
  found: FoundSkill
  checked: boolean
  onChange: (on: boolean) => void
}) {
  const { skill } = found
  return (
    <li className="flex items-start gap-2.5 rounded-(--radius-item) border px-3 py-2.5">
      <Checkbox
        id={id}
        className="mt-0.5"
        checked={checked}
        onCheckedChange={(on) => onChange(Boolean(on))}
      />
      <label htmlFor={id} className="flex min-w-0 flex-1 flex-col gap-1">
        <span className="flex flex-wrap items-center gap-2">
          <span className="font-mono text-xs font-medium text-foreground">
            {skill.name}
          </span>
          {found.existing && (
            <Chip tone="warning">Replaces the vault&apos;s</Chip>
          )}
        </span>
        <span className="line-clamp-2 text-2xs/[1.6] text-muted-foreground">
          {skill.description}
        </span>
        <span className="text-2xs text-subtle">
          {found.folder || "Top folder"} ·{" "}
          {skill.files.length === 1 ? "1 file" : `${skill.files.length} files`}{" "}
          · {formatBytes(skillBytes(skill))}
        </span>
      </label>
    </li>
  )
}
