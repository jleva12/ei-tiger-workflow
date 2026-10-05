import * as React from "react"

import {
  AssistantProvider,
  AssistantThread,
  useAdkAssistant,
} from "@/components/forge/assistant/index"
import { Icon } from "@/components/forge/icon"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { useMe } from "@/lib/users"
import { RUNTIME_URL, type ChatAgentRecord } from "@/features/agents/lib/api"
import { declaredState } from "@/features/agents/lib/input"
import type { DataType } from "@/features/steps/lib/types"

/**
 * Talking to the agent as a chat would: the runtime's run API, the version
 * the builder shows (`ca_x@draft`, `ca_x@3`, or `ca_x`), as the signed-in
 * person. The state the agent declares is filled in here and sent with
 * every message, as the chat's `stateDelta`.
 */
export function ChatAgentTestPanel({
  record,
  appName,
  onClose,
}: {
  record: ChatAgentRecord
  appName: string
  onClose: () => void
}) {
  const me = useMe()
  const userId = me.data?.subject ?? "tester"
  const { fields } = React.useMemo(() => {
    const entry = record.document.nodes.find((n) => n.kind === "agent")
    return declaredState(
      entry?.kind === "agent" ? entry.config.state_schema : {}
    )
  }, [record.document])
  const [values, setValues] = React.useState<Record<string, string>>({})
  // What the next message sends, read as it's sent.
  const latest = React.useRef(values)
  React.useEffect(() => {
    latest.current = values
  })

  return (
    <aside
      aria-label={`Test ${record.document.name}`}
      className="fixed top-16 right-4 bottom-4 z-30 flex w-[min(28rem,calc(100vw-2rem))] flex-col overflow-hidden rounded-(--radius-card) border bg-background shadow-(--shadow-float)"
    >
      <header className="flex items-start gap-3 border-b px-4 pt-3 pb-2.5">
        <div className="flex min-w-0 flex-1 flex-col">
          <h2 className="truncate text-sm font-medium text-foreground">
            Test {record.document.name || "the agent"}
          </h2>
          <p className="truncate text-2xs text-muted-foreground">
            As {userId}, through the runtime:{" "}
            <code className="font-mono">appName {appName}</code>
          </p>
        </div>
        <Button
          variant="ghost"
          size="icon-sm"
          aria-label="Close the test chat"
          onClick={onClose}
          className="-mr-1.5 text-muted-foreground"
        >
          <Icon icon="close" />
        </Button>
      </header>
      {fields.length > 0 && (
        <section className="flex flex-col gap-2 border-b px-4 py-2.5">
          <p className="text-2xs text-muted-foreground">
            State sent with each message (stateDelta)
          </p>
          {fields.map(([name, type]) => (
            <label key={name} className="flex items-center gap-2 text-xs">
              <code className="w-32 shrink-0 truncate font-mono text-foreground">
                {name}
              </code>
              <StateInput
                type={type}
                value={values[name] ?? ""}
                onChange={(value) => setValues({ ...values, [name]: value })}
              />
            </label>
          ))}
        </section>
      )}
      <div className="min-h-0 flex-1">
        {RUNTIME_URL ? (
          <TestChat
            key={`${appName}:${userId}`}
            appName={appName}
            userId={userId}
            name={record.document.name}
            state={() => stateDelta(fields, latest.current)}
          />
        ) : (
          <p className="p-4 text-xs text-muted-foreground">
            Set VITE_API_URL to talk to agents through the runtime.
          </p>
        )}
      </div>
    </aside>
  )
}

function TestChat({
  appName,
  userId,
  name,
  state,
}: {
  appName: string
  userId: string
  name: string
  state: () => Record<string, unknown>
}) {
  const { runtime, artifacts } = useAdkAssistant({
    adk: { url: RUNTIME_URL!, appName, userId },
    runState: state,
  })
  return (
    <AssistantProvider
      runtime={runtime}
      artifacts={artifacts}
      title={name || "Agent"}
      welcome={{
        title: `Talk to ${name || "the agent"}`,
        description:
          "Each message runs it as a chat would, with the state above.",
      }}
    >
      <AssistantThread />
    </AssistantProvider>
  )
}

function StateInput({
  type,
  value,
  onChange,
}: {
  type: DataType
  value: string
  onChange: (value: string) => void
}) {
  if (type.kind === "string" && type.enum?.length) {
    return (
      <select
        className="h-7 min-w-0 flex-1 rounded-(--radius-control) border bg-background px-2 text-xs"
        value={value}
        onChange={(event) => onChange(event.target.value)}
      >
        <option value="">Not sent</option>
        {type.enum.map((option) => (
          <option key={option} value={option}>
            {option}
          </option>
        ))}
      </select>
    )
  }
  if (type.kind === "boolean") {
    return (
      <select
        className="h-7 min-w-0 flex-1 rounded-(--radius-control) border bg-background px-2 text-xs"
        value={value}
        onChange={(event) => onChange(event.target.value)}
      >
        <option value="">Not sent</option>
        <option value="true">true</option>
        <option value="false">false</option>
      </select>
    )
  }
  return (
    <Input
      className="h-7 min-w-0 flex-1 text-xs"
      value={value}
      placeholder={
        type.kind === "object" || type.kind === "array" ? "JSON" : "Not sent"
      }
      onChange={(event) => onChange(event.target.value)}
    />
  )
}

/** The filled-in fields, as their types: what the chat sends in stateDelta. */
function stateDelta(
  fields: [string, DataType][],
  values: Record<string, string>
): Record<string, unknown> {
  const out: Record<string, unknown> = {}
  for (const [name, type] of fields) {
    const raw = values[name]
    if (raw === undefined || raw === "") continue
    switch (type.kind) {
      case "number":
        if (!Number.isNaN(Number(raw))) out[name] = Number(raw)
        break
      case "boolean":
        out[name] = raw === "true"
        break
      case "object":
      case "array":
      case "unknown":
        try {
          out[name] = JSON.parse(raw)
        } catch {
          out[name] = raw
        }
        break
      default:
        out[name] = raw
    }
  }
  return out
}
