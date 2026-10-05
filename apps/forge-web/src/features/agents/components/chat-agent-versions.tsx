import { LockIcon } from "@hugeicons/core-free-icons"

import { Icon } from "@/components/forge/icon"
import { Chip } from "@/components/forge/status"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Spinner } from "@/components/ui/spinner"
import { toast } from "@/components/ui/toast"
import { RUNTIME_URL, type ChatAgentRecord } from "@/features/agents/lib/api"

/*
 * Where a chat agent is between draft and published, as its builder shows
 * it: the chip by its name, the read-only banner on a published version, the
 * publish dialog, and how to run it.
 */

/** "Draft v1", "Published v3", "Viewing v2". */
export function VersionChip({
  record,
  viewing,
}: {
  record: ChatAgentRecord
  /** An older published version open read-only. */
  viewing?: number
}) {
  if (viewing !== undefined)
    return <Chip tone="neutral">Viewing v{viewing}</Chip>
  if (record.has_draft)
    return <Chip tone="notice">Draft v{record.draft_version}</Chip>
  return <Chip tone="success">Published v{record.published_version}</Chip>
}

/** Over a published version's canvas: it can't change; a new version can. */
export function ReadOnlyBanner({
  version,
  latest,
  canManage,
  hasDraft,
  pending,
  onNewVersion,
  onOpenCurrent,
}: {
  version: number
  /** The agent's latest published version. */
  latest: number | null
  canManage: boolean
  hasDraft: boolean
  pending: boolean
  onNewVersion: () => void
  onOpenCurrent: () => void
}) {
  const old = latest !== null && version !== latest
  return (
    <div className="pointer-events-none absolute inset-x-0 top-3 z-10 flex justify-center px-3">
      <div className="pointer-events-auto flex max-w-full items-center gap-3 rounded-(--radius-card) border bg-background py-1.5 pr-1.5 pl-3 text-xs shadow-(--shadow-float)">
        <Icon
          icon={LockIcon}
          size={14}
          className="shrink-0 text-muted-foreground"
        />
        <span className="min-w-0 truncate text-muted-foreground">
          <span className="font-medium text-foreground">
            {old ? `Version ${version}` : `Published v${version}`}
          </span>{" "}
          is read-only: published versions never change.
        </span>
        {old || hasDraft ? (
          <Button size="xs" variant="outline" onClick={onOpenCurrent}>
            {hasDraft ? "Open the draft" : "Open the latest"}
          </Button>
        ) : (
          canManage && (
            <Button size="xs" onClick={onNewVersion} disabled={pending}>
              {pending && <Spinner data-icon="inline-start" />}
              New version
            </Button>
          )
        )}
      </div>
    </div>
  )
}

/** Confirms publishing: it can't be changed afterwards. */
export function PublishDialog({
  open,
  onOpenChange,
  record,
  name,
  errors,
  problems,
  pending,
  onPublish,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  record: ChatAgentRecord
  name: string
  /** The builder's own errors: it can't run with them. */
  errors: number
  /** Why the API refused it, after trying. */
  problems: string[]
  pending: boolean
  onPublish: () => void
}) {
  const version = record.draft_version ?? 1
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>
            Publish {name || "the agent"} as version {version}?
          </DialogTitle>
          <DialogDescription>
            Version {version} is frozen as it is now: it can&apos;t be changed
            afterwards, only replaced by a new version.{" "}
            {record.published_version
              ? `Calls to ${record.id} switch from v${record.published_version} to it; ${record.id}@${record.published_version} keeps running v${record.published_version}.`
              : `Calls to ${record.id} run it.`}
          </DialogDescription>
        </DialogHeader>
        {errors > 0 && (
          <p className="flex items-start gap-2 text-xs/[1.6] text-destructive">
            <Icon icon="warning" size={14} className="mt-0.5 shrink-0" />
            It has {errors === 1 ? "an error" : `${errors} errors`} to fix
            first: see the issues in the header.
          </p>
        )}
        {problems.length > 0 && (
          <ul className="flex flex-col gap-1 text-xs/[1.6] text-destructive">
            {problems.map((problem) => (
              <li key={problem} className="flex items-start gap-2">
                <Icon icon="warning" size={14} className="mt-0.5 shrink-0" />
                <span>{problem}</span>
              </li>
            ))}
          </ul>
        )}
        <DialogFooter>
          <DialogClose render={<Button type="button" variant="outline" />}>
            Cancel
          </DialogClose>
          <Button disabled={pending || errors > 0} onClick={onPublish}>
            {pending && <Spinner data-icon="inline-start" />}
            Publish v{version}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function Copyable({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex flex-col gap-0.5">
      <span className="text-2xs text-muted-foreground">{label}</span>
      <div className="flex items-center gap-1">
        <code className="min-w-0 flex-1 truncate rounded-(--radius-control) border bg-muted/40 px-1.5 py-1 font-mono text-2xs text-foreground">
          {value}
        </code>
        <Button
          variant="ghost"
          size="icon-xs"
          aria-label={`Copy ${label.toLowerCase()}`}
          onClick={() => {
            void navigator.clipboard?.writeText(value)
            toast.add({
              title: `Copied ${label.toLowerCase()}`,
              type: "success",
            })
          }}
        >
          <Icon icon="copy" />
        </Button>
      </div>
    </div>
  )
}

/**
 * How to talk to the agent: the runtime's address, and the app name each
 * version answers to (ADK's run API: POST run_sse).
 */
export function RunInfo({ record }: { record: ChatAgentRecord }) {
  const endpoint = RUNTIME_URL
    ? new URL(`${RUNTIME_URL}/run_sse`, window.location.origin).toString()
    : "/api/v1/runtime/run_sse"
  return (
    <section className="flex flex-col gap-2.5 border-b px-4 py-3.5">
      <div className="flex items-center justify-between gap-2">
        <h3 className="text-xs font-medium text-foreground">Run it</h3>
        <VersionChip record={record} />
      </div>
      <p className="text-xs/[1.6] text-muted-foreground">
        Any app can talk to it over ADK&apos;s run API: create a session, then
        post each message with its <code className="font-mono">appName</code>.
      </p>
      <Copyable label="Endpoint" value={endpoint} />
      {record.published_version !== null && (
        <Copyable
          label={`appName: latest published (v${record.published_version})`}
          value={record.id}
        />
      )}
      {record.has_draft && (
        <Copyable label="appName: this draft" value={`${record.id}@draft`} />
      )}
    </section>
  )
}
