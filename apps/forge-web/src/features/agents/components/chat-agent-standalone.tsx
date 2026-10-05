import * as React from "react"
import { keepPreviousData, useQuery } from "@tanstack/react-query"
import { GithubIcon, PackageIcon } from "@hugeicons/core-free-icons"

import { ErrorCallout } from "@/components/forge/feedback"
import { Icon } from "@/components/forge/icon"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { DialogClose } from "@/components/ui/dialog"
import { Skeleton } from "@/components/ui/skeleton"
import { Spinner } from "@/components/ui/spinner"
import { toast } from "@/components/ui/toast"
import {
  CheckField,
  ChoiceField,
  Row,
  TextField,
} from "@/features/builder/components/fields/basic-fields"
import { IssueList } from "@/features/builder/components/issue-list"
import {
  SettingsBody,
  SettingsDialog,
  SettingsFooter,
  SettingsGlyph,
  SettingsHeader,
  SettingsHint,
  SettingsSection,
} from "@/features/builder/components/settings-dialog"
import { downloadBlob } from "@/features/builder/components/utils"
import { toApiError } from "@/lib/api/index"
import {
  DEFAULT_STARTER_OPTIONS,
  downloadStandalone,
  previewStandalone,
  publishProblems,
  type ChatAgentDetail,
  type StandaloneRequest,
  type StarterOptions,
} from "@/features/agents/lib/api"

type Version = number | "draft"
type Choice<T extends string> = { value: T; label: string; about: string }

const INTERFACES: Choice<StarterOptions["interface"]>[] = [
  {
    value: "ui",
    label: "Chat UI and API",
    about:
      "A blank page with the assistant in its corner, served with the API on one port.",
  },
  {
    value: "api",
    label: "API only",
    about: "ADK's run API alone, for your own front end or services to call.",
  },
]
const SESSIONS: Choice<StarterOptions["sessions"]>[] = [
  {
    value: "memory",
    label: "In memory",
    about: "Gone when the server restarts.",
  },
  {
    value: "sqlite",
    label: "SQLite",
    about: "A file in data/, with nothing else to run.",
  },
  {
    value: "postgresql",
    label: "PostgreSQL",
    about: "At DATABASE_URL. compose.yaml runs one for development.",
  },
  {
    value: "mysql",
    label: "MySQL",
    about: "At DATABASE_URL. compose.yaml runs one for development.",
  },
]
const ARTIFACTS: Choice<StarterOptions["artifacts"]>[] = [
  {
    value: "memory",
    label: "In memory",
    about:
      "What the agent's tools save (ADK artifacts), gone when the server restarts.",
  },
  {
    value: "folder",
    label: "A folder",
    about: "What the agent's tools save, in data/artifacts beside the server.",
  },
  {
    value: "s3",
    label: "Amazon S3, or any S3",
    about:
      "What the agent's tools save, in a bucket at ARTIFACTS_URL. compose.yaml runs one for development.",
  },
]
const MEMORY: Choice<StarterOptions["memory"]>[] = [
  {
    value: "memory",
    label: "In memory",
    about:
      "What the Memory tool remembers, matched by shared words and gone on restart.",
  },
  {
    value: "atlas",
    label: "MongoDB Atlas",
    about:
      "What the Memory tool remembers, searched by meaning with OpenAI embeddings.",
  },
]

/**
 * Generate standalone agent: the agent as a project of its own, as Spring
 * Initializr makes a Spring Boot app, set up in the same settings card as a
 * step. Pick the version (or the draft), where it keeps conversations, files
 * and memories, how it replies, whether it has the chat UI, who may call it
 * and who owns it; what's in it follows as you go. Nothing in it calls Forge.
 */
export function StandaloneDialog({
  open,
  onOpenChange,
  record,
  shownVersion,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  record: ChatAgentDetail
  /** The version the builder shows; the draft when it shows the draft. */
  shownVersion: number | undefined
}) {
  return (
    <SettingsDialog open={open} onOpenChange={onOpenChange}>
      <StandaloneSettings
        open={open}
        onDone={() => onOpenChange(false)}
        record={record}
        shownVersion={shownVersion}
      />
    </SettingsDialog>
  )
}

function StandaloneSettings({
  open,
  onDone,
  record,
  shownVersion,
}: {
  open: boolean
  onDone: () => void
  record: ChatAgentDetail
  shownVersion: number | undefined
}) {
  const versions = React.useMemo<{ value: string; label: string }[]>(
    () => [
      ...(record.has_draft
        ? [
            {
              value: "draft",
              label: `Draft v${record.draft_version}, as last saved`,
            },
          ]
        : []),
      ...record.versions.map((v) => ({
        value: String(v.version),
        label: `Version ${v.version}${v.version === record.published_version ? " (latest)" : ""}`,
      })),
    ],
    [record]
  )
  const [version, setVersion] = React.useState<Version>(
    shownVersion ??
      (record.has_draft ? "draft" : (record.published_version ?? "draft"))
  )
  const [name, setName] = React.useState("")
  const [options, setOptions] = React.useState<StarterOptions>(
    DEFAULT_STARTER_OPTIONS
  )
  const [origins, setOrigins] = React.useState("")
  const [owners, setOwners] = React.useState("")
  const [downloading, setDownloading] = React.useState(false)
  const set = <K extends keyof StarterOptions>(
    key: K,
    value: StarterOptions[K]
  ) => setOptions((current) => ({ ...current, [key]: value }))
  const ui = options.interface === "ui"

  // What's typed reaches the preview once typing stops.
  const typed = useDebounced({ name, origins, owners }, 400)
  const body: StandaloneRequest = {
    version,
    ...(typed.name.trim() ? { name: typed.name.trim() } : {}),
    options: {
      ...options,
      // A page can't keep a key secret: only the API alone asks for one.
      api_key: options.api_key && !ui,
      cors_origins: ui ? [] : words(typed.origins),
      code_owners: words(typed.owners),
    },
  }
  const preview = useQuery({
    queryKey: [
      "chat-agents",
      record.organization_id,
      record.id,
      "standalone",
      body,
    ],
    queryFn: () => previewStandalone(record.organization_id, record.id, body),
    enabled: open,
    retry: false,
    placeholderData: keepPreviousData,
  })
  const projectName = slug(name.trim() || preview.data?.name || "agent")
  const settled =
    typed.name === name && typed.origins === origins && typed.owners === owners
  const notes = (preview.data?.notes ?? []).map((note, index) => ({
    id: `note-${index}`,
    level: "warning" as const,
    message: note,
  }))
  const shown =
    version === "draft"
      ? `draft v${record.draft_version}`
      : `version ${version}`

  const download = async () => {
    setDownloading(true)
    try {
      const zip = await downloadStandalone(
        record.organization_id,
        record.id,
        body
      )
      downloadBlob(zip, `${projectName}.zip`)
      toast.add({
        title: `Downloaded ${projectName}.zip`,
        description: "Unzip it, fill in .env, and see its README to run it.",
        type: "success",
      })
      onDone()
    } catch (caught) {
      toast.add({
        title: "Couldn't generate the project",
        description: message(caught),
        type: "error",
      })
    } finally {
      setDownloading(false)
    }
  }

  return (
    <>
      <SettingsHeader
        glyph={<SettingsGlyph icon={PackageIcon} tone="action" />}
        title="Generate standalone agent"
        description={`${record.document.name || "Agent"} · ${shown}`}
        closeLabel="Close the standalone agent's settings"
      />

      <SettingsBody>
        <SettingsSection className="gap-3">
          <p className="text-xs/[1.6] text-muted-foreground">
            A Python server built on forge-agent-runtime, made of what you pick
            here, ready to run once its .env is filled in. Nothing in it calls
            Forge.
          </p>
          {preview.error ? (
            <ErrorCallout title={message(preview.error)}>
              {publishProblems(preview.error).length > 1 && (
                <ul className="flex flex-col gap-0.5">
                  {publishProblems(preview.error).map((problem) => (
                    <li key={problem}>{problem}</li>
                  ))}
                </ul>
              )}
            </ErrorCallout>
          ) : (
            notes.length > 0 && <IssueList issues={notes} within />
          )}
        </SettingsSection>

        <SettingsSection>
          <Row>
            <ChoiceField
              label="Version"
              value={String(version)}
              options={versions}
              onChange={(next) =>
                setVersion(next === "draft" ? "draft" : Number(next))
              }
            />
            <TextField
              label="Name"
              value={name}
              placeholder={preview.data?.name ?? "support-assistant"}
              onChange={setName}
            />
          </Row>
          <p className="-mt-2 text-xs/[1.6] text-muted-foreground">
            It unzips to{" "}
            <code className="rounded-(--radius-chip) bg-muted px-1 py-px font-mono text-2xs text-foreground">
              {projectName}/
            </code>
            , and that's its package's name.
          </p>
        </SettingsSection>

        <SettingsSection title="Interface">
          <Picked
            label="Interface"
            choices={INTERFACES}
            value={options.interface}
            onChange={(value) => set("interface", value)}
          />
        </SettingsSection>

        <SettingsSection title="Where it keeps things">
          <Picked
            label="Conversations"
            choices={SESSIONS}
            value={options.sessions}
            onChange={(value) => set("sessions", value)}
          />
          <Picked
            label="Files"
            choices={ARTIFACTS}
            value={options.artifacts}
            onChange={(value) => set("artifacts", value)}
          />
          <Picked
            label="Long-term memory"
            choices={MEMORY}
            value={options.memory}
            onChange={(value) => set("memory", value)}
          />
        </SettingsSection>

        <SettingsSection title="Replies and access">
          <CheckField
            label="Stream replies as the model writes them"
            checked={options.streaming}
            onChange={(checked) => set("streaming", checked)}
            description={
              options.streaming
                ? "Words appear as they're written."
                : "Each reply arrives whole, once the model is done."
            }
          />
          <CheckField
            label="Ask callers for an API key"
            checked={options.api_key && !ui}
            disabled={ui}
            onChange={(checked) => set("api_key", checked)}
            description={
              ui
                ? "With the API only: a page can't keep a key secret. Put the chat UI behind your own sign-in instead."
                : "Callers send Authorization: Bearer <key>, one of AGENT_API_KEYS."
            }
          />
          {!ui && (
            <TextField
              label="Pages that may call it"
              value={origins}
              placeholder="https://app.example.com"
              onChange={setOrigins}
              code
              description="Origins, separated by spaces or commas. Browsers on other origins are refused (CORS)."
            />
          )}
        </SettingsSection>

        <SettingsSection title="Code owners">
          <TextField
            label="Who reviews changes"
            value={owners}
            placeholder="@your-org/your-team"
            onChange={setOwners}
            code
            description="GitHub users, @org/team or emails, separated by spaces, for .github/CODEOWNERS."
          />
        </SettingsSection>

        <SettingsSection>
          <h3 className="-mb-1 flex items-center gap-2 text-xs font-medium text-foreground">
            GitHub workflows
            <Badge variant="outline">Coming soon</Badge>
          </h3>
          <p className="text-xs/[1.6] text-muted-foreground">
            The project&apos;s CI and deploy workflows, set up here: checks on
            every pull request, an image on every release.
          </p>
          <Button
            type="button"
            variant="outline"
            size="sm"
            disabled
            className="w-full border-dashed text-muted-foreground"
          >
            <Icon icon={GithubIcon} data-icon="inline-start" />
            Add a workflow
          </Button>
        </SettingsSection>

        <SettingsSection>
          <div className="-mb-1 flex items-center justify-between gap-2">
            <h3 className="text-xs font-medium text-foreground">
              What&apos;s in it
            </h3>
            {(preview.isFetching || !settled) && preview.data && (
              <Spinner className="size-3 text-muted-foreground" />
            )}
          </div>
          {preview.isPending ? (
            <Skeleton className="h-48 w-full rounded-(--radius-control)" />
          ) : preview.data ? (
            <>
              <ProjectTree name={projectName} files={preview.data.files} />
              {preview.data.env.length > 0 && (
                <p className="text-xs/[1.8] text-muted-foreground">
                  Its .env needs{" "}
                  {preview.data.env.map((key, index) => (
                    <React.Fragment key={key}>
                      {index > 0 && " "}
                      <code className="rounded-(--radius-chip) bg-muted px-1 py-px font-mono text-2xs text-foreground">
                        {key}
                      </code>
                    </React.Fragment>
                  ))}
                  {options.sessions !== "memory" ||
                  options.artifacts !== "memory" ||
                  options.memory !== "memory"
                    ? "; where it keeps things is filled in for compose.yaml or data/."
                    : "."}
                </p>
              )}
            </>
          ) : null}
        </SettingsSection>
      </SettingsBody>

      <SettingsFooter>
        <SettingsHint>
          {preview.data
            ? `${preview.data.files.length} files · the runtime ${
                preview.data.runtime === "wheels"
                  ? "comes with it"
                  : `from PyPI ${preview.data.runtime.slice(5)}`
              }`
            : "Nothing in it calls Forge"}
        </SettingsHint>
        <DialogClose
          render={
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="max-[600px]:ml-auto"
            />
          }
        >
          Cancel
        </DialogClose>
        <Button
          type="button"
          size="sm"
          disabled={!preview.isSuccess || !settled || downloading}
          onClick={() => void download()}
        >
          {downloading ? (
            <Spinner data-icon="inline-start" />
          ) : (
            <Icon icon="download" data-icon="inline-start" />
          )}
          Download .zip
        </Button>
      </SettingsFooter>
    </>
  )
}

/** One of a few choices, as a step's settings pick one, saying what it means. */
function Picked<T extends string>({
  label,
  choices,
  value,
  onChange,
}: {
  label: string
  choices: Choice<T>[]
  value: T
  onChange: (value: T) => void
}) {
  return (
    <ChoiceField
      label={label}
      hideLabel={label === "Interface"}
      value={value}
      options={choices}
      onChange={onChange}
      description={choices.find((choice) => choice.value === value)?.about}
    />
  )
}

/** A value once it has stopped changing for `ms`. */
function useDebounced<T>(value: T, ms: number): T {
  const [settled, setSettled] = React.useState(value)
  const key = JSON.stringify(value)
  React.useEffect(() => {
    const timer = window.setTimeout(() => setSettled(value), ms)
    return () => window.clearTimeout(timer)
    // The value is compared by what it holds, not which object it is.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, ms])
  return settled
}

/** Space- or comma-separated words. */
const words = (text: string) => text.split(/[\s,]+/).filter(Boolean)

/** An error's message, without FastAPI's "Value error, " in front. */
const message = (error: unknown) =>
  toApiError(error).message.replace(/^Value error, /, "")

/** A name as the project's folder: lower case, dashes. */
const slug = (name: string) =>
  name
    .toLowerCase()
    .normalize("NFKD")
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "") || "agent"

/**
 * What's in it, as a step's settings list rows: folders first, folded to
 * their file counts beyond the top, then the project's own files.
 */
function ProjectTree({ name, files }: { name: string; files: string[] }) {
  const rows = React.useMemo(() => {
    const folders = new Map<string, number>()
    const top: string[] = []
    for (const file of files) {
      const parts = file.split("/")
      if (parts.length === 1) top.push(file)
      else {
        // web/src/components/... folds into web/src/components/
        const folder = parts.slice(0, Math.min(parts.length - 1, 3)).join("/")
        folders.set(folder, (folders.get(folder) ?? 0) + 1)
      }
    }
    const byPath = (a: string, b: string) => a.localeCompare(b)
    return [
      ...[...folders.keys()].sort(byPath).map((folder) => {
        const count = folders.get(folder) ?? 0
        return {
          path: `${folder}/`,
          folder: true,
          note: `${count} file${count === 1 ? "" : "s"}`,
        }
      }),
      ...top.sort(byPath).map((path) => ({ path, folder: false, note: "" })),
    ]
  }, [files])
  return (
    <ul
      aria-label={`${name}/`}
      className="flex flex-col rounded-(--radius-control) border"
    >
      <li className="flex items-center gap-2 px-2.5 py-1.5">
        <Icon
          icon={PackageIcon}
          size={14}
          className="shrink-0 text-muted-foreground"
        />
        <code className="min-w-0 truncate font-mono text-xs font-medium text-foreground">
          {name}/
        </code>
      </li>
      {rows.map((row) => (
        <li
          key={row.path}
          className="flex items-center gap-2 border-t py-1.5 pr-2.5 pl-7"
        >
          <Icon
            icon={row.folder ? "folder" : "file"}
            size={13}
            className="shrink-0 text-subtle"
          />
          <code className="min-w-0 truncate font-mono text-xs text-foreground">
            {row.path}
          </code>
          {row.note && (
            <span className="ml-auto shrink-0 font-mono text-2xs text-muted-foreground tabular-nums">
              {row.note}
            </span>
          )}
        </li>
      ))}
    </ul>
  )
}
