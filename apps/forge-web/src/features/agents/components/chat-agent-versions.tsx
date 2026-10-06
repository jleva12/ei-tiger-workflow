import { Copyable } from "@/components/forge/copyable"
import {
  a2aCardAddress,
  runtimeAddress,
  type ChatAgentRecord,
} from "@/features/agents/lib/api"
import { RuntimeAccess } from "@/features/api-keys/components/runtime-access"
import { VersionChip } from "@/features/builder/components/versions"

// The version pieces are shared with the workflows' builder.
export {
  PublishDialog,
  ReadOnlyBanner,
  VersionChip,
} from "@/features/builder/components/versions"

/*
 * How to run a chat agent, as its builder shows it; and, from the builder
 * kit (builder/components/versions), where it is between draft and
 * published: the chip by its name, the read-only banner, the publish dialog.
 */

/**
 * How to talk to the agent: the runtime's address and the app name each
 * version answers to (ADK's run API: POST run_sse), and each one's A2A card
 * (Google's A2A protocol, 1.0 and 0.3), which says where to call it.
 */
export function RunInfo({ record }: { record: ChatAgentRecord }) {
  const endpoint = runtimeAddress("/run_sse")
  const organizationId = record.organization_id
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
      <RuntimeAccess organizationId={organizationId} />
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
      <p className="pt-1 text-xs/[1.6] text-muted-foreground">
        Other agents can call it over Google&apos;s A2A protocol (JSON-RPC, 1.0
        and 0.3): give them its card, which says where and how.
      </p>
      {record.published_version !== null && (
        <Copyable
          label={`A2A card: latest published (v${record.published_version})`}
          value={a2aCardAddress(record.id)}
        />
      )}
      {record.has_draft && (
        <Copyable
          label="A2A card: this draft"
          value={a2aCardAddress(`${record.id}@draft`)}
        />
      )}
    </section>
  )
}
