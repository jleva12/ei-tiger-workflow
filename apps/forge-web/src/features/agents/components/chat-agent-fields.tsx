import * as React from "react"

import {
  CheckField,
  ChoiceField,
  OptionalNumberField,
  TextField,
} from "@/features/builder/components/fields/basic-fields"
import { IssueMessages } from "@/features/builder/components/fields/field-issues"
import { useFieldIssues } from "@/features/builder/components/fields/field-issues-context"
import { ExpressionField } from "@/features/builder/components/fields/expression-field"
import { ModelFields } from "@/features/builder/components/fields/model-fields"
import { Field, FieldDescription, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { adkName } from "@/features/adk-workflows/lib/model"
import { chatScope } from "@/features/agents/lib/input"
import {
  AGENT_LIKE,
  type ChatAgentSettings,
  type ChatConfigs,
  type ChatKind,
  type ChatStep,
} from "@/features/agents/lib/model"
import { useChatBuilder } from "./chat-agent-store"
import { InputSchemaField } from "./input-schema-field"
import { ToolFields, type ToolKind } from "./tool-fields"

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

const NO_STATE: Record<string, unknown> = {}

/**
 * An agent's instruction, a template completed and checked against what
 * the chat sends: the run request, and the state the chat agent declares.
 */
function InstructionField({
  value,
  onChange,
}: {
  value: string
  onChange: (value: string) => void
}) {
  const stateSchema = useChatBuilder((s) => {
    const entry = s.nodes.find((n) => n.data.kind === "agent")?.data
    return entry?.kind === "agent" ? entry.config.state_schema : NO_STATE
  })
  const scope = React.useMemo(() => chatScope(stateSchema), [stateSchema])
  // Its issues, but for its template's: it checks those itself.
  const { issues, key } = useFieldIssues("instruction")
  return (
    <ExpressionField
      label="Instruction"
      value={value}
      onChange={onChange}
      mode="template"
      check={{ textual: true }}
      scope={scope}
      multiline
      rows={7}
      placeholder="Who it is, what it does, and how it answers. {{ opens a reference."
      description={
        <>
          Its system instruction: what it&apos;s told before every conversation.{" "}
          {code("{{ state.… }}")} puts in what the chat sends,{" "}
          {code("{{ request.… }}")} who&apos;s chatting.
        </>
      }
      issues={issues.filter((i) => !i.id.includes(":field:"))}
      field={key}
    />
  )
}

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
      <InstructionField
        value={config.instruction}
        onChange={(value) => set("instruction", value)}
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
  const set = useSet<"agent">(id)
  return (
    <>
      <LlmSettings id={id} config={config} />
      <InputSchemaField
        schema={config.state_schema}
        onChange={(schema) => set("state_schema", schema)}
      />
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

/* -------------------------------------------------------------------------- */
/* Tools                                                                      */
/* -------------------------------------------------------------------------- */

/* -------------------------------------------------------------------------- */
/* By kind                                                                    */
/* -------------------------------------------------------------------------- */

/** A tool node's settings: the shared tool fields, on the builder's store. */
function ChatToolFields<K extends ToolKind>({
  id,
  kind,
  config,
}: {
  id: string
  kind: K
  config: ChatConfigs[K]
}) {
  const set = useSet<K>(id)
  const updateStep = useChatBuilder((s) => s.updateStep)
  const self = useChatBuilder((s) => s.meta.id)
  const lookups = useChatBuilder((s) => s.lookups)
  const patch = React.useCallback(
    (values: Partial<ChatConfigs[K]>, key: string) =>
      updateStep(
        id,
        (step) =>
          ({ ...step, config: { ...step.config, ...values } }) as ChatStep,
        key
      ),
    [id, updateStep]
  )
  return (
    <ToolFields
      kind={kind}
      bind={{ config, set, patch }}
      lookups={lookups}
      self={self}
      confirmAbout="The chat asks before the call is made, showing what the model wants to send."
    />
  )
}

function kindFields(id: string, step: ChatStep) {
  switch (step.kind) {
    case "agent":
      return <AgentFields id={id} config={step.config} />
    case "sub_agent":
      return <SubAgentFields id={id} config={step.config} />
    default:
      return <ChatToolFields id={id} kind={step.kind} config={step.config} />
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
