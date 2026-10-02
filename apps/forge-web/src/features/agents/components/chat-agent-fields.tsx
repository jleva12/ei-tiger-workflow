import * as React from "react"
import { Delete02Icon } from "@hugeicons/core-free-icons"

import {
  CheckField,
  ChoiceField,
  CODE,
  OptionalNumberField,
  TextField,
} from "@/features/builder/components/fields/basic-fields"
import { IssueMessages } from "@/features/builder/components/fields/field-issues"
import { useFieldIssues } from "@/features/builder/components/fields/field-issues-context"
import { ModelFields } from "@/features/builder/components/fields/model-fields"
import { SchemaField } from "@/features/builder/components/fields/schema-field"
import { Icon } from "@/components/forge/icon"
import { Button } from "@/components/ui/button"
import { Field, FieldDescription, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { adkName } from "@/features/adk-workflows/lib/model"
import {
  AGENT_LIKE,
  type ChatAgentSettings,
  type ChatConfigs,
  type ChatKind,
  type ChatStep,
} from "@/features/agents/lib/model"
import { HTTP_METHODS, uid, type HeaderRow } from "@/features/steps/lib/model"
import { useChatBuilder } from "./chat-agent-store"

/*
 * The settings of each kind of node on a chat agent's canvas, as its
 * settings dialog shows them. Every change goes to the builder's store as
 * it's typed; typing in one field is one step of undo. What's attached to
 * an agent is drawn on the canvas, not listed here.
 */

const code = (text: string) => (
  <code className="font-mono text-[0.9em]">{text}</code>
)

type Props<K extends ChatKind> = { id: string; config: ChatConfigs[K] }

/** Sets one of a node's settings; typing in it is one step of undo. */
function useSet<K extends ChatKind>(id: string) {
  const updateStep = useChatBuilder((s) => s.updateStep)
  return React.useCallback(
    <F extends keyof ChatConfigs[K]>(field: F, value: ChatConfigs[K][F]) =>
      updateStep(
        id,
        (step) =>
          ({ ...step, config: { ...step.config, [field]: value } }) as ChatStep,
        String(field)
      ),
    [id, updateStep]
  )
}

/** A node's name, and what ADK (or the model) calls it by. */
function NodeName({ id, step }: { id: string; step: ChatStep }) {
  const updateStep = useChatBuilder((s) => s.updateStep)
  const nameId = React.useId()
  const name = adkName(step.name)
  const { issues, invalid, key } = useFieldIssues("name")
  const said = AGENT_LIKE.has(step.kind) ? (
    name ? (
      <>ADK calls it {code(name)}.</>
    ) : (
      "Its name needs a letter for ADK to call it by."
    )
  ) : step.kind === "http_tool" ? (
    name ? (
      <>The model calls it {code(name)}.</>
    ) : (
      "Its name is the tool's: it needs a letter for the model to call it by."
    )
  ) : (
    "What the canvas calls it."
  )
  return (
    <Field data-invalid={invalid || undefined} data-field={key}>
      <FieldLabel htmlFor={nameId}>Name</FieldLabel>
      <Input
        id={nameId}
        aria-invalid={invalid || undefined}
        value={step.name}
        onChange={(event) =>
          updateStep(
            id,
            (current) => ({ ...current, name: event.target.value }),
            "name"
          )
        }
      />
      {issues.length ? (
        <IssueMessages issues={issues} />
      ) : (
        <FieldDescription>{said}</FieldDescription>
      )}
    </Field>
  )
}

/* -------------------------------------------------------------------------- */
/* Agents                                                                     */
/* -------------------------------------------------------------------------- */

/** An LLM agent's own settings: the chat agent's, or a sub-agent's. */
function LlmSettings({
  id,
  config,
}: {
  id: string
  config: ChatAgentSettings
}) {
  const set = useSet<"agent">(id)
  const updateStep = useChatBuilder((s) => s.updateStep)
  return (
    <>
      <TextField
        issue="description"
        label="What it's for"
        value={config.description}
        multiline
        rows={2}
        placeholder="Answers customers' questions about their orders."
        onChange={(value) => set("description", value)}
        description="Agents read this when they hand off to it, or call it."
      />
      <TextField
        issue="instruction"
        label="Instruction"
        value={config.instruction}
        multiline
        rows={7}
        placeholder="Who it is, what it does, and how it answers."
        onChange={(value) => set("instruction", value)}
        description="Its system instruction: what it's told before every conversation."
      />
      <ModelFields
        model={config.model}
        thinkingLevel={config.thinking_level}
        onChange={(values, key) =>
          updateStep(
            id,
            (step) =>
              ({ ...step, config: { ...step.config, ...values } }) as ChatStep,
            key
          )
        }
      />
      <OptionalNumberField
        issue="max_output_tokens"
        label="Most tokens"
        value={config.max_output_tokens}
        min={1}
        integer
        placeholder="Model's limit"
        onChange={(value) => set("max_output_tokens", value)}
      />
    </>
  )
}

function AgentFields({ id, config }: Props<"agent">) {
  return (
    <>
      <LlmSettings id={id} config={config} />
      <p className="text-xs/[1.6] text-muted-foreground">
        Connect its <span className="font-medium text-foreground">Tools</span>{" "}
        way to what it can call, and its{" "}
        <span className="font-medium text-foreground">Hands off to</span> way to
        the agents it can pass the conversation to.
      </p>
    </>
  )
}

const HAND_OFFS: { value: ChatConfigs["sub_agent"]["mode"]; label: string }[] =
  [
    { value: "chat", label: "It takes over the conversation" },
    {
      value: "task",
      label: "It does a task, asking if it must, then hands back",
    },
    { value: "single_turn", label: "It answers once, then hands back" },
  ]

function SubAgentFields({ id, config }: Props<"sub_agent">) {
  const set = useSet<"sub_agent">(id)
  return (
    <>
      <LlmSettings id={id} config={config} />
      <ChoiceField
        issue="mode"
        label="When it's handed off to"
        value={config.mode}
        options={HAND_OFFS}
        onChange={(value) => set("mode", value)}
        description="On a Tools way it's called like a tool instead, and answers the agent that called it."
      />
      <CheckField
        label="Sees only what it's given"
        checked={config.include_contents === "none"}
        onChange={(on) => set("include_contents", on ? "none" : "default")}
        description="Not the conversation so far: for agents that work on one thing at a time."
      />
      {config.mode === "chat" && (
        <>
          <CheckField
            label="Doesn't hand back to the agent that handed off"
            checked={config.disallow_transfer_to_parent}
            onChange={(on) => set("disallow_transfer_to_parent", on)}
          />
          <CheckField
            label="Doesn't hand off to the other sub-agents"
            checked={config.disallow_transfer_to_peers}
            onChange={(on) => set("disallow_transfer_to_peers", on)}
          />
        </>
      )}
    </>
  )
}

function SavedAgentFields({ id, config }: Props<"saved_agent">) {
  const set = useSet<"saved_agent">(id)
  const self = useChatBuilder((s) => s.meta.id)
  const agents = useChatBuilder((s) => s.lookups.agents)
  const others = Object.entries(agents).filter(([agentId]) => agentId !== self)
  return (
    <ChoiceField
      issue="agent"
      label="Agent"
      value={config.agent}
      placeholder={others.length ? "Pick an agent" : "No other agents yet"}
      options={others.map(([value, label]) => ({ value, label }))}
      onChange={(value) => set("agent", value)}
      description="It's used whole: with its own instruction, tools and hand-offs."
    />
  )
}

function WorkflowFields({ id, config }: Props<"adk_workflow">) {
  const set = useSet<"adk_workflow">(id)
  const workflows = useChatBuilder((s) => s.lookups.workflows)
  const options = Object.entries(workflows).map(([value, label]) => ({
    value,
    label,
  }))
  return (
    <ChoiceField
      issue="workflow"
      label="ADK workflow"
      value={config.workflow}
      placeholder={
        options.length ? "Pick an ADK workflow" : "No ADK workflows yet"
      }
      options={options}
      onChange={(value) => set("workflow", value)}
      description="The agent runs it with its start's input fields and reads its result. Its description tells the agent when to."
    />
  )
}

/* -------------------------------------------------------------------------- */
/* Tools                                                                      */
/* -------------------------------------------------------------------------- */

function MemoryFields({ id, config }: Props<"memory">) {
  const set = useSet<"memory">(id)
  return (
    <ChoiceField
      issue="mode"
      label="It remembers"
      value={config.mode}
      options={[
        { value: "on_demand", label: "When it decides to look something up" },
        {
          value: "every_turn",
          label: "With every message: what's relevant comes along",
        },
      ]}
      onChange={(value) => set("mode", value)}
      description="Memory is what was said in the person's earlier conversations with it."
    />
  )
}

/** Headers: a row each, its name and value side by side and a bin to remove it. */
function HeaderRows({
  headers,
  onChange,
}: {
  headers: HeaderRow[]
  onChange: (headers: HeaderRow[]) => void
}) {
  const [added, setAdded] = React.useState<string>()
  const { issues, invalid, key } = useFieldIssues("headers")
  const change = (header: HeaderRow, patch: Partial<HeaderRow>) =>
    onChange(headers.map((x) => (x.id === header.id ? { ...x, ...patch } : x)))
  return (
    <div
      role="group"
      aria-label="Headers"
      data-field={key}
      className="flex flex-col gap-2"
    >
      <span className="text-xs font-medium text-foreground">Headers</span>
      {headers.map((header) => (
        <div
          key={header.id}
          className="grid grid-cols-[minmax(0,2fr)_minmax(0,3fr)_auto] items-center gap-1.5"
        >
          <Input
            aria-label="Header name"
            value={header.name}
            placeholder="Key"
            spellCheck={false}
            autoComplete="off"
            autoFocus={header.id === added}
            aria-invalid={(invalid && !header.name.trim()) || undefined}
            onChange={(event) => change(header, { name: event.target.value })}
            className={CODE}
          />
          <Input
            aria-label={`${header.name.trim() || "Header"} value`}
            value={header.value}
            placeholder="Value"
            spellCheck={false}
            autoComplete="off"
            onChange={(event) => change(header, { value: event.target.value })}
            className={CODE}
          />
          <Button
            type="button"
            variant="ghost"
            size="icon"
            aria-label={`Remove ${header.name.trim() || "header"}`}
            onClick={() => onChange(headers.filter((x) => x.id !== header.id))}
            className="text-muted-foreground hover:text-destructive"
          >
            <Icon icon={Delete02Icon} />
          </Button>
        </div>
      ))}
      <Button
        type="button"
        variant="outline"
        size="sm"
        onClick={() => {
          const header = { id: uid("header"), name: "", value: "" }
          setAdded(header.id)
          onChange([...headers, header])
        }}
        className="w-full border-dashed text-muted-foreground"
      >
        <Icon icon="plus" data-icon="inline-start" />
        Add header
      </Button>
      <IssueMessages issues={issues} />
    </div>
  )
}

const CONFIRM = "A person confirms each call"
const CONFIRM_ABOUT =
  "The chat asks before the call is made, showing what the model wants to send."

function HttpToolFields({ id, config }: Props<"http_tool">) {
  const set = useSet<"http_tool">(id)
  return (
    <>
      <TextField
        issue="description"
        label="What it does"
        value={config.description}
        multiline
        rows={2}
        placeholder="Finds an order by its number: its items, status and delivery date."
        onChange={(value) => set("description", value)}
        description="The model reads this to decide when to call it."
      />
      <div className="grid grid-cols-[7rem_minmax(0,1fr)] items-start gap-2.5">
        <ChoiceField
          label="Method"
          value={config.method}
          options={HTTP_METHODS.map((m) => ({ value: m, label: m }))}
          onChange={(value) => set("method", value)}
        />
        <TextField
          issue="url"
          label="URL"
          value={config.url}
          code
          placeholder="https://api.example.com/orders/{order_id}"
          onChange={(value) => set("url", value)}
        />
      </div>
      <HeaderRows
        headers={config.headers}
        onChange={(value) => set("headers", value)}
      />
      <SchemaField
        issue="parameters"
        label="What the model sends"
        title="The arguments the model fills in"
        description={
          <>
            Its fields and their types. A field named in the URL as{" "}
            {code("{name}")} goes there; the rest go in the query (GET, DELETE)
            or the JSON body.
          </>
        }
        schema={config.parameters}
        onChange={(schema) => set("parameters", schema)}
        empty="Not declared: the model calls it with no arguments."
        subject={{ one: "argument", many: "arguments" }}
      />
      <CheckField
        label={CONFIRM}
        checked={config.confirm}
        onChange={(on) => set("confirm", on)}
        description={CONFIRM_ABOUT}
      />
    </>
  )
}

function OpenApiFields({ id, config }: Props<"openapi">) {
  const set = useSet<"openapi">(id)
  return (
    <>
      <ChoiceField
        label="The spec"
        value={config.source}
        options={[
          { value: "url", label: "From a URL" },
          { value: "inline", label: "Pasted here" },
        ]}
        onChange={(value) => set("source", value)}
      />
      {config.source === "url" ? (
        <TextField
          issue="url"
          label="Spec URL"
          value={config.url}
          code
          placeholder="https://api.example.com/openapi.json"
          onChange={(value) => set("url", value)}
          description="An OpenAPI 3 spec, JSON or YAML."
        />
      ) : (
        <TextField
          issue="spec"
          label="Spec"
          value={config.spec}
          code
          multiline
          rows={10}
          placeholder={'{\n  "openapi": "3.0.0",\n  "paths": { … }\n}'}
          onChange={(value) => set("spec", value)}
          description="An OpenAPI 3 spec, JSON or YAML."
        />
      )}
      <TextField
        issue="operations"
        label="Operations it may call"
        value={config.operations}
        code
        placeholder="getInvoice, listCharges"
        onChange={(value) => set("operations", value)}
        description="Their operation IDs, separated by commas; empty for every one. Each is a tool, named after it."
      />
      <CheckField
        label={CONFIRM}
        checked={config.confirm}
        onChange={(on) => set("confirm", on)}
        description={CONFIRM_ABOUT}
      />
    </>
  )
}

function McpFields({ id, config }: Props<"mcp">) {
  const set = useSet<"mcp">(id)
  return (
    <>
      <div className="grid grid-cols-[10rem_minmax(0,1fr)] items-start gap-2.5">
        <ChoiceField
          label="Transport"
          value={config.transport}
          options={[
            { value: "streamable_http", label: "Streamable HTTP" },
            { value: "sse", label: "SSE" },
          ]}
          onChange={(value) => set("transport", value)}
        />
        <TextField
          issue="url"
          label="Server URL"
          value={config.url}
          code
          placeholder="https://mcp.example.com/mcp"
          onChange={(value) => set("url", value)}
        />
      </div>
      <HeaderRows
        headers={config.headers}
        onChange={(value) => set("headers", value)}
      />
      <TextField
        issue="tools"
        label="Tools it may call"
        value={config.tools}
        code
        placeholder="search_articles, get_article"
        onChange={(value) => set("tools", value)}
        description="The server's tool names, separated by commas; empty for every one."
      />
      <CheckField
        label={CONFIRM}
        checked={config.confirm}
        onChange={(on) => set("confirm", on)}
        description={CONFIRM_ABOUT}
      />
    </>
  )
}

/* -------------------------------------------------------------------------- */
/* By kind                                                                    */
/* -------------------------------------------------------------------------- */

function kindFields(id: string, step: ChatStep) {
  switch (step.kind) {
    case "agent":
      return <AgentFields id={id} config={step.config} />
    case "sub_agent":
      return <SubAgentFields id={id} config={step.config} />
    case "saved_agent":
      return <SavedAgentFields id={id} config={step.config} />
    case "adk_workflow":
      return <WorkflowFields id={id} config={step.config} />
    case "memory":
      return <MemoryFields id={id} config={step.config} />
    case "http_tool":
      return <HttpToolFields id={id} config={step.config} />
    case "openapi":
      return <OpenApiFields id={id} config={step.config} />
    case "mcp":
      return <McpFields id={id} config={step.config} />
  }
}

/** The settings of a node, for its kind. */
export function ChatAgentFields({ id, step }: { id: string; step: ChatStep }) {
  return (
    <>
      <NodeName id={id} step={step} />
      {kindFields(id, step)}
    </>
  )
}
