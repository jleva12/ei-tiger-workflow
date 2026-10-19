import * as React from "react"
import { useQueryClient } from "@tanstack/react-query"

import {
  CheckField,
  ChoiceField,
  OptionalNumberField,
  TextField,
} from "@/features/builder/components/fields/basic-fields"
import { IssueMessages } from "@/features/builder/components/fields/field-issues"
import { useFieldIssues } from "@/features/builder/components/fields/field-issues-context"
import { MarkdownTemplateField } from "@/features/builder/components/fields/markdown-template-field"
import { ModelFields } from "@/features/builder/components/fields/model-fields"
import { Field, FieldDescription, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { adkName } from "@/features/adk-workflows/lib/model"
import { chatScope } from "@/features/agents/lib/input"
import {
  isAgentLike,
  type Guarded,
  type ChatAgentSettings,
  type ChatConfigs,
  type ChatKind,
  type ChatStep,
} from "@/features/agents/lib/model"
import { instructionEditor } from "@/features/agents/lib/instruction-writer"
import { chatInstructionBrief } from "./chat-agent-brief"
import { useChatBuilder, useChatBuilderApi } from "./chat-agent-store"
import { InputSchemaField } from "./input-schema-field"
import { IntakeAgentFields } from "./intake-agent-fields"
import { InstructionWriter } from "./instruction-writer"
import { AccessRuleField } from "./access-rule-field"
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
  const said = isAgentLike(step.kind) ? (
    name ? (
      <>ADK calls it {code(name)}.</>
    ) : (
      "Its name needs a letter for ADK to call it by."
    )
  ) : step.kind === "rich_answers" ? (
    <>The model calls it {code("present")}.</>
  ) : step.kind === "http_tool" || step.kind === "code_execution" ? (
    name || step.kind === "code_execution" ? (
      <>The model calls it {code(name || "run_code")}.</>
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

/** What a template reads in a chat: the run request, and the state the chat agent declares. */
function useChatScope() {
  const stateSchema = useChatBuilder((s) => {
    const entry = s.nodes.find((n) => n.data.kind === "agent")?.data
    return entry?.kind === "agent" ? entry.config.state_schema : NO_STATE
  })
  return React.useMemo(() => chatScope(stateSchema), [stateSchema])
}

/**
 * An agent's instruction, a Markdown template checked against what the chat
 * sends: the run request, and the state the chat agent declares. Its first
 * lines show here; Open editor writes it in the rich text editor in a card
 * beside. Its ✨ has the instruction writer write it, for the agent as it's
 * set up.
 */
function InstructionField({
  id,
  value,
  onChange,
  writer = true,
  placeholder = "Who it is, what it does, and how it answers.",
  description,
}: {
  /** The agent's node. */
  id: string
  value: string
  onChange: (value: string) => void
  /** Its ✨ has the instruction writer write it. */
  writer?: boolean
  placeholder?: string
  description?: React.ReactNode
}) {
  const scope = useChatScope()
  const store = useChatBuilderApi()
  const client = useQueryClient()
  const organizationId = useChatBuilder((s) => s.meta.organization_id)
  const name = useChatBuilder(
    (s) => s.nodes.find((n) => n.id === id)?.data.name
  )
  // Its issues, but for its template's: it checks those itself.
  const { issues, key } = useFieldIssues("instruction")
  return (
    <MarkdownTemplateField
      label="Instruction"
      value={value}
      onChange={onChange}
      writer={
        writer
          ? ({ value: current, onApply }) => (
              <InstructionWriter
                organizationId={organizationId}
                value={current}
                onApply={onApply}
                brief={() => chatInstructionBrief(store.getState(), id, client)}
              />
            )
          : undefined
      }
      ai={
        organizationId
          ? instructionEditor(
              organizationId,
              writer
                ? () => chatInstructionBrief(store.getState(), id, client)
                : undefined
            )
          : undefined
      }
      scope={scope}
      check={INSTRUCTION_CHECK}
      placeholder={placeholder}
      editorTitle={
        name?.trim() ? `${name.trim()}'s instruction` : "Instruction"
      }
      editorDescription={
        <>
          What it&apos;s told before every conversation, in Markdown.{" "}
          {code("{{ state.… }}")} puts in what the chat sends,{" "}
          {code("{{ request.… }}")} who&apos;s chatting.
        </>
      }
      description={
        description ?? (
          <>
            Its system instruction: what it&apos;s told before every
            conversation. {code("{{ state.… }}")} puts in what the chat sends,{" "}
            {code("{{ request.… }}")} who&apos;s chatting.
          </>
        )
      }
      issues={issues.filter((i) => !i.id.includes(":field:"))}
      field={key}
    />
  )
}

/** A template writes text: an object in it reads as JSON. */
const INSTRUCTION_CHECK = { textual: true }

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
        id={id}
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
      <CheckField
        label="Can ask the person"
        checked={config.ask_person}
        onChange={(on) => set("ask_person", on)}
        description="It may stop to ask something only the person can tell it, and the chat shows a form for the answer. Not when a workflow runs it."
      />
    </>
  )
}

function AgentFields({ id, config, name }: Props<"agent"> & { name: string }) {
  const set = useSet<"agent">(id)
  return (
    <>
      <LlmSettings id={id} config={config} />
      <InputSchemaField
        schema={config.state_schema}
        onChange={(schema) => set("state_schema", schema)}
      />
      <AccessFields id={id} config={config} name={name} />
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

/**
 * An intake agent's model settings: its tone (an optional instruction; the
 * intake's own is worked out from its design), its model and whether it may
 * ask with a form.
 */
function IntakeModelSettings({
  id,
  config,
}: {
  id: string
  config: ChatConfigs["intake_agent"]
}) {
  const set = useSet<"intake_agent">(id)
  const updateStep = useChatBuilder((s) => s.updateStep)
  return (
    <>
      <InstructionField
        id={id}
        value={config.instruction}
        onChange={(value) => set("instruction", value)}
        writer={false}
        placeholder="Warm and brief. Explain any jargon."
        description="Optional: its tone, and anything else it should know. What to collect, what's in and what to ask next are added for it before every call."
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
      <CheckField
        label="Asks with forms"
        checked={config.ask_person}
        onChange={(on) => set("ask_person", on)}
        description="For a choice, a date or a yes or no it may show a form in the chat instead of asking in words."
      />
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
  const organizationId = useChatBuilder((s) => s.meta.organization_id)
  const scope = useChatScope()
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
      organizationId={organizationId}
      self={self}
      confirmAbout="The chat asks before the call is made, showing what the model wants to send."
      showToPersonAbout="People chatting with the agent see what it was called with and what it answered. Otherwise they see only that it ran; you see both when you test it."
      scope={scope}
      valuesAbout={
        <>
          what the chat sends ({code("{{ state.… }}")}) or who&apos;s chatting (
          {code("{{ request.… }}")})
        </>
      }
    />
  )
}

function kindFields(id: string, step: ChatStep) {
  switch (step.kind) {
    case "agent":
      return <AgentFields id={id} config={step.config} name={step.name} />
    case "sub_agent":
      return <SubAgentFields id={id} config={step.config} />
    case "intake_agent":
      return (
        <IntakeAgentFields
          id={id}
          config={step.config}
          model={<IntakeModelSettings id={id} config={step.config} />}
        />
      )
    default:
      return <ChatToolFields id={id} kind={step.kind} config={step.config} />
  }
}

/** Who may use an agent, of any kind on the canvas: the rule on its node. */
function AccessFields({
  id,
  config,
  name,
}: {
  id: string
  config: Guarded
  name: string
}) {
  const set = useSet<"agent">(id)
  const organizationId = useChatBuilder((s) => s.meta.organization_id)
  if (!organizationId) return null
  return (
    <AccessRuleField
      organizationId={organizationId}
      label={name}
      rule={config.access}
      onChange={(rule) => set("access", rule)}
    />
  )
}

/** The settings of a node, for its kind. */
export function ChatAgentFields({ id, step }: { id: string; step: ChatStep }) {
  return (
    <>
      <NodeName id={id} step={step} />
      {kindFields(id, step)}
      {/* The chat agent's is among its own settings, above the canvas hint. */}
      {step.kind !== "agent" && "access" in step.config && (
        <AccessFields id={id} config={step.config} name={step.name} />
      )}
    </>
  )
}
