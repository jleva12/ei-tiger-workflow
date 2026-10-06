import { Copyable } from "@/components/forge/copyable"
import { RuntimeAccess } from "@/features/api-keys/components/runtime-access"
import { VersionChip } from "@/features/builder/components/versions"
import type { AgentRecord } from "@/features/adk-workflows/lib/api"
import { sampleInput } from "@/features/adk-workflows/lib/samples"
import { a2aCardAddress, runtimeAddress } from "@/lib/runtime"
import { useAgentBuilder } from "./agent-store"

/**
 * How other apps run the workflow: the runtime's REST API (start a run,
 * wait for it, answer what it waits at) for the reference each version
 * answers to, and each one's A2A card (Google's A2A protocol, 1.0 and 0.3).
 */
export function WorkflowRunInfo({
  record,
  viewing,
}: {
  record: AgentRecord
  /** A published version open read-only. */
  viewing?: number
}) {
  const schema = useAgentBuilder((s) => {
    const start = s.nodes.find((n) => n.data.kind === "start")?.data
    return start?.kind === "start" ? (start.config.input_schema ?? {}) : {}
  })
  // The reference it's called by: the version shown, else the latest published, else the draft.
  const ref =
    viewing !== undefined
      ? `${record.id}@${viewing}`
      : record.published_version !== null
        ? record.id
        : `${record.id}@draft`
  const runs = runtimeAddress(`/workflows/${ref}/runs`)
  const curl = [
    `curl ${runs} \\`,
    "  -H 'Authorization: Bearer fk_…' \\",
    "  -H 'Content-Type: application/json' \\",
    `  -d '${JSON.stringify({ input: sampleInput(schema), wait: 30 })}'`,
  ].join("\n")
  return (
    <section className="flex flex-col gap-2.5 border-b px-4 py-3.5">
      <div className="flex items-center justify-between gap-2">
        <h3 className="text-xs font-medium text-foreground">
          Run it from other apps
        </h3>
        <VersionChip record={record} viewing={viewing} />
      </div>
      <p className="text-xs/[1.6] text-muted-foreground">
        Start a run with its input; with <code className="font-mono">wait</code>
        , the answer comes when it pauses or ends. Follow it at the run&apos;s{" "}
        <code className="font-mono">links.self</code>, and answer what it waits
        at there. <code className="font-mono">{record.id}</code> runs the latest
        published version; <code className="font-mono">@3</code> pins one,{" "}
        <code className="font-mono">@draft</code> runs the draft.
      </p>
      <RuntimeAccess organizationId={record.organization_id} />
      <Copyable label="Runs" value={runs} />
      <Copyable label="Try it" value={curl} multiline />
      {record.has_draft && ref !== `${record.id}@draft` && (
        <Copyable
          label="Runs: this draft"
          value={runtimeAddress(`/workflows/${record.id}@draft/runs`)}
        />
      )}
      <p className="pt-1 text-xs/[1.6] text-muted-foreground">
        Other agents can run it over Google&apos;s A2A protocol (JSON-RPC, 1.0
        and 0.3): a task is a run, and when it waits for an answer or an
        approval the task asks for it.
      </p>
      <Copyable label="A2A card" value={a2aCardAddress(ref)} />
    </section>
  )
}
