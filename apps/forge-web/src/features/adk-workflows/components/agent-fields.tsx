import * as React from "react"
import { Delete02Icon } from "@hugeicons/core-free-icons"
import { cn } from "cn"

import {
  CheckField,
  ChoiceField,
  NumberField,
  OptionalNumberField,
  TextField,
} from "@/features/builder/components/fields/basic-fields"
import { ModelFields } from "@/features/builder/components/fields/model-fields"
import { SchemaField } from "@/features/builder/components/fields/schema-field"
import { KindGlyph } from "@/features/builder/components/glyph"
import { Icon } from "@/components/forge/icon"
import { Button } from "@/components/ui/button"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { Field, FieldDescription, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import {
  agentExpressionSetting,
  subAgentInstruction,
} from "@/features/adk-workflows/lib/fields"
import {
  adkName,
  AGENT_KINDS,
  isForgeKind,
  namesIn,
  newSubAgent,
  SUB_AGENT_KINDS,
  subAgentAt,
  subAgentsOf,
  updateSubAgent,
  walkSubAgents,
  type AgentConfigs,
  type AgentKind,
  type AgentStep,
  type LlmSettings,
  type LoopSettings,
  type SubAgent,
  type SubAgentKind,
  type TeamSettings,
} from "@/features/adk-workflows/lib/model"
import type { StepData } from "@/features/steps/lib/model"
import {
  FieldIssuesPrefix,
  IssueMessages,
} from "@/features/builder/components/fields/field-issues"
import {
  useFieldIssues,
  useIssueLookup,
} from "@/features/builder/components/fields/field-issues-context"
import { ExpressionField } from "@/features/builder/components/fields/expression-field"
import { StepFields } from "@/features/adk-workflows/components/step-fields"
import { subAgentLine } from "./agent-lines"
import { useAgentBuilder } from "./agent-store"

/*
 * The settings of each kind of node, as its settings dialog shows them.
 * Every change goes to the builder's store as it's typed; typing in one
 * field is one step of undo. An agent's sub-agents are listed in its
 * settings; opening one shows its settings in the same dialog, with the
 * way back above them. Forge's steps show the workflow builder's fields
 * (they read the same store, so they complete against the agent's data).
 */

/**
 * Settings to edit: a node's, or a sub-agent's inside it. `patch` changes
 * several at once under one undo key (the model and its thinking level).
 */
type Bind<C> = {
  config: C
  set<F extends keyof C>(field: F, value: C[F]): void
  patch(patch: Partial<C>, key: string): void
}

/** A node's own settings, as a Bind. */
function useNodeBind<K extends AgentKind>(
  id: string,
  config: AgentConfigs[K]
): Bind<AgentConfigs[K]> {
  const updateStep = useAgentBuilder((s) => s.updateStep)
  return React.useMemo(() => {
    const patch = (values: Partial<AgentConfigs[K]>, key: string) =>
      updateStep(
        id,
        (step) =>
          ({ ...step, config: { ...step.config, ...values } }) as AgentStep,
        key
      )
    return {
      config,
      patch,
      set: (field, value) =>
        patch(
          { [field]: value } as unknown as Partial<AgentConfigs[K]>,
          String(field)
        ),
    }
  }, [id, config, updateStep])
}

/** A sub-agent's settings, found by its path inside the node, as a Bind. */
function useSubAgentBind(
  id: string,
  path: string[],
  agent: SubAgent
): Bind<SubAgent["config"]> {
  const updateStep = useAgentBuilder((s) => s.updateStep)
  return React.useMemo(() => {
    const patch = (values: Partial<SubAgent["config"]>, key: string) =>
      updateStep(
        id,
        (step) =>
          updateSubAgent(
            step,
            path,
            (a) => ({ ...a, config: { ...a.config, ...values } }) as SubAgent
          ),
        `${path.join("/")}:${key}`
      )
    return {
      config: agent.config,
      patch,
      set: (field, value) =>
        patch(
          { [field]: value } as unknown as Partial<SubAgent["config"]>,
          String(field)
        ),
    }
  }, [id, path, agent.config, updateStep])
}

/**
 * A setting that reads the run's data, completed and checked against what
 * the node can read. How it's written and what it must come to come from
 * the node's kind (adk-workflows/lib/fields.ts), the same as the agent's issues.
 */
function Setting({
  id,
  setting,
  ...field
}: Omit<
  React.ComponentProps<typeof ExpressionField>,
  "mode" | "scope" | "check"
> & {
  id: string
  /** Its key in adk-workflows/lib/fields.ts. */
  setting: string
}) {
  const scopeOf = useAgentBuilder((s) => s.scopeOf)
  const step = useAgentBuilder((s) => s.nodes.find((n) => n.id === id)?.data)
  const scope = React.useMemo(() => scopeOf(id), [scopeOf, id])
  const spec = React.useMemo(
    () => (step ? agentExpressionSetting(step, setting) : undefined),
    [step, setting]
  )
  // Its issues, but for its expression's: it checks those itself.
  const { issues, key } = useFieldIssues(setting, { whole: true })
  if (!spec) return null
  return (
    <ExpressionField
      {...field}
      mode={spec.mode}
      scope={scope}
      check={spec.check}
      issues={issues.filter((i) => !i.id.includes(":field:"))}
      field={key}
    />
  )
}

const code = (text: string) => (
  <code className="font-mono text-[0.9em]">{text}</code>
)

/* -------------------------------------------------------------------------- */
/* Sub-agents                                                                 */
/* -------------------------------------------------------------------------- */

/**
 * An agent's sub-agents: a row each, opening its settings, moved and
 * removed in place; a menu adds one of any kind.
 */
function SubAgentList({
  label,
  agents,
  ordered,
  onChange,
  onOpen,
  description,
  issue,
}: {
  label: string
  agents: SubAgent[]
  /** The order they run in matters: rows can move. */
  ordered: boolean
  onChange: (agents: SubAgent[]) => void
  onOpen: (id: string) => void
  description?: React.ReactNode
  /** The setting it holds, for its issues. */
  issue?: string
}) {
  const { issues: own, key } = useFieldIssues(issue)
  // A sub-agent's issues, and its sub-agents': its row says so, since its
  // settings open on their own.
  const whole = useIssueLookup({ whole: true })
  const issuesOf = (agent: SubAgent) =>
    [agent, ...[...walkSubAgents(agent.config.sub_agents)].map((a) => a.agent)]
      .flatMap((a) => whole.under(`agents.${a.id}`))
      .sort((a, b) => (a.level === b.level ? 0 : a.level === "error" ? -1 : 1))
  const lookups = useAgentBuilder((s) => s.lookups)
  const nodes = useAgentBuilder((s) => s.nodes)
  const move = (from: number, to: number) => {
    const next = [...agents]
    const [moved] = next.splice(from, 1)
    next.splice(to, 0, moved)
    onChange(next)
  }
  const add = (kind: SubAgentKind) => {
    const agent = newSubAgent(kind, namesIn(nodes.map((n) => n.data)))
    onChange([...agents, agent])
    onOpen(agent.id)
  }
  return (
    <div
      role="group"
      aria-label={label}
      data-field={key}
      className="flex flex-col gap-2"
    >
      <span className="text-xs font-medium text-foreground">{label}</span>
      {agents.map((agent, index) => {
        const issues = issuesOf(agent)
        const error = issues.some((i) => i.level === "error")
        return (
          <div
            key={agent.id}
            data-field={whole.key(`agents.${agent.id}`)}
            className={cn(
              "flex items-center gap-1 rounded-(--radius-control) border py-1 pr-1 pl-1.5",
              error && "border-destructive"
            )}
          >
            <button
              type="button"
              onClick={() => onOpen(agent.id)}
              className="flex min-w-0 flex-1 items-center gap-2.5 rounded-(--radius-item) px-1 py-0.5 text-left transition-colors duration-150 hover:bg-muted"
            >
              {ordered && (
                <span className="w-3 shrink-0 text-center text-2xs text-subtle tabular-nums">
                  {index + 1}
                </span>
              )}
              <KindGlyph info={AGENT_KINDS[agent.kind]} size="sm" />
              <span className="flex min-w-0 flex-1 flex-col">
                <span className="truncate text-xs font-medium text-foreground">
                  {agent.name.trim() || "Unnamed"}
                </span>
                {issues.length ? (
                  <span
                    className={cn(
                      "flex items-center gap-1 truncate text-2xs",
                      error ? "text-destructive" : "text-tone-amber-foreground"
                    )}
                  >
                    <Icon
                      icon={error ? "failed" : "warning"}
                      size={12}
                      className="shrink-0"
                    />
                    <span className="truncate">
                      {issues.length > 1
                        ? `${issues.length} to fix: ${issues[0].message}`
                        : issues[0].message}
                    </span>
                  </span>
                ) : (
                  <span className="truncate text-2xs text-muted-foreground">
                    {AGENT_KINDS[agent.kind].label} ·{" "}
                    {subAgentLine(agent, lookups)}
                  </span>
                )}
              </span>
              <Icon icon="right" size={14} className="shrink-0 text-subtle" />
            </button>
            {ordered && (
              <>
                <Button
                  type="button"
                  variant="ghost"
                  size="icon-xs"
                  aria-label={`Move ${agent.name} up`}
                  disabled={index === 0}
                  onClick={() => move(index, index - 1)}
                >
                  <Icon icon="up" />
                </Button>
                <Button
                  type="button"
                  variant="ghost"
                  size="icon-xs"
                  aria-label={`Move ${agent.name} down`}
                  disabled={index === agents.length - 1}
                  onClick={() => move(index, index + 1)}
                >
                  <Icon icon="down" />
                </Button>
              </>
            )}
            <Button
              type="button"
              variant="ghost"
              size="icon-xs"
              aria-label={`Remove ${agent.name}`}
              onClick={() => onChange(agents.filter((a) => a.id !== agent.id))}
              className="text-muted-foreground hover:text-destructive"
            >
              <Icon icon={Delete02Icon} />
            </Button>
          </div>
        )
      })}
      <DropdownMenu>
        <DropdownMenuTrigger
          render={
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="w-full border-dashed text-muted-foreground"
            />
          }
        >
          <Icon icon="plus" data-icon="inline-start" />
          Add a sub-agent
        </DropdownMenuTrigger>
        <DropdownMenuContent align="start" className="w-72">
          <DropdownMenuGroup>
            <DropdownMenuLabel>A sub-agent that…</DropdownMenuLabel>
            {SUB_AGENT_KINDS.map((kind) => (
              <DropdownMenuItem
                key={kind}
                onClick={() => add(kind)}
                className="items-start"
              >
                <KindGlyph info={AGENT_KINDS[kind]} size="sm" />
                <span className="flex min-w-0 flex-col">
                  <span>{AGENT_KINDS[kind].label}</span>
                  <span className="text-2xs text-muted-foreground">
                    {AGENT_KINDS[kind].summary}
                  </span>
                </span>
              </DropdownMenuItem>
            ))}
          </DropdownMenuGroup>
        </DropdownMenuContent>
      </DropdownMenu>
      {own.length ? (
        <IssueMessages issues={own} />
      ) : (
        description && (
          <p className="text-xs/[1.5] text-muted-foreground">{description}</p>
        )
      )}
    </div>
  )
}

/* -------------------------------------------------------------------------- */
/* Agents                                                                     */
/* -------------------------------------------------------------------------- */

/** An LLM agent's settings: a node's (`node`), or a sub-agent's. */
function LlmFields({
  id,
  instruction,
  bind,
  node,
  onOpen,
}: {
  /** The node it's in (or is). */
  id: string
  /** Its instruction's key among the node's settings (adk-workflows/lib/fields.ts). */
  instruction: string
  bind: Bind<LlmSettings & Partial<Pick<AgentConfigs["llm"], "mode">>>
  /** It's a node of the graph, not a sub-agent: it has a mode. */
  node: boolean
  onOpen: (id: string) => void
}) {
  const { config, set, patch } = bind
  return (
    <>
      <TextField
        label="What it's for"
        value={config.description}
        multiline
        rows={2}
        placeholder="Handles billing questions: invoices, charges and refunds."
        onChange={(value) => set("description", value)}
        description="Other agents read this when deciding to hand off to it."
      />
      <Setting
        id={id}
        setting={instruction}
        label="Instruction"
        value={config.instruction}
        multiline
        rows={6}
        placeholder="What the agent should do, and how."
        onChange={(value) => set("instruction", value)}
        description={
          <>
            Type {code("{{")} to put in the run&apos;s data as it runs: the
            input, what an earlier node handed on ({code("steps")}), or what a
            sub-agent answered ({code("state")}).
          </>
        }
      />
      <ModelFields
        model={config.model}
        thinkingLevel={config.thinking_level}
        onChange={(values, key) => patch(values, key)}
      />
      {node && config.mode && (
        <ChoiceField
          issue="mode"
          label="It works"
          value={config.mode}
          options={[
            {
              value: "single_turn",
              label: "Answers once, with what it's given",
            },
            {
              value: "task",
              label: "Until its task is done, asking back if it must",
            },
          ]}
          onChange={(value) => set("mode", value)}
        />
      )}
      <SchemaField
        issue="output_schema"
        label="What it answers"
        title="The JSON the agent answers with"
        description="Its fields and their types. The agent is held to them, and later nodes read them, completed and checked."
        schema={config.output_schema}
        onChange={(schema) => set("output_schema", schema)}
        empty="Not declared: it answers in text."
        subject={{ one: "answer", many: "answers" }}
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
        label="Sees only what it's given"
        checked={config.include_contents === "none"}
        onChange={(on) => set("include_contents", on ? "none" : "default")}
        description="Not the conversation so far: for agents that work on one thing at a time."
      />
      <SubAgentList
        issue="sub_agents"
        label="Hands off to"
        agents={config.sub_agents}
        ordered={false}
        onChange={(agents) => set("sub_agents", agents)}
        onOpen={onOpen}
        description="Agents it can pass the conversation to, choosing by what each is for."
      />
      {!node && (
        <>
          <CheckField
            label="Doesn't hand back to its parent"
            checked={config.disallow_transfer_to_parent}
            onChange={(on) => set("disallow_transfer_to_parent", on)}
          />
          <CheckField
            label="Doesn't hand off to its peers"
            checked={config.disallow_transfer_to_peers}
            onChange={(on) => set("disallow_transfer_to_peers", on)}
          />
        </>
      )}
    </>
  )
}

type TeamKind = "sequential" | "parallel" | "loop_agent"

const TEAM_WORDS: Record<TeamKind, { list: string; about: string }> = {
  sequential: {
    list: "Runs, in order",
    about:
      "Each runs after the one before, and can read what the ones before answered as state.<their ID>.",
  },
  parallel: {
    list: "Runs, at once",
    about:
      "They run side by side, each on its own; each answer is kept as state.<its ID>.",
  },
  loop_agent: {
    list: "Runs, in order, each pass",
    about:
      "It goes round until the most passes, or until one of them says it's done.",
  },
}

/** A Sequential, Parallel or Loop agent's settings, as a node or a sub-agent. */
function TeamFields({
  bind,
  kind,
  onOpen,
}: {
  bind: Bind<TeamSettings & Partial<Pick<LoopSettings, "max_iterations">>>
  kind: TeamKind
  onOpen: (id: string) => void
}) {
  const { config, set } = bind
  return (
    <>
      <TextField
        label="What it's for"
        value={config.description}
        multiline
        rows={2}
        placeholder="Reproduces the bug, then drafts a fix."
        onChange={(value) => set("description", value)}
        description="Other agents read this when deciding to hand off to it."
      />
      {kind === "loop_agent" && config.max_iterations !== undefined && (
        <NumberField
          issue="max_iterations"
          label="Most passes"
          value={config.max_iterations}
          min={1}
          onChange={(value) => set("max_iterations", value)}
        />
      )}
      <SubAgentList
        issue="sub_agents"
        label={TEAM_WORDS[kind].list}
        agents={config.sub_agents}
        ordered={kind !== "parallel"}
        onChange={(agents) => set("sub_agents", agents)}
        onOpen={onOpen}
        description={TEAM_WORDS[kind].about}
      />
    </>
  )
}

/** A sub-agent's settings, opened from its agent's list. */
function SubAgentFields({
  id,
  path,
  agent,
  onOpen,
}: {
  id: string
  path: string[]
  agent: SubAgent
  onOpen: (id: string) => void
}) {
  const updateStep = useAgentBuilder((s) => s.updateStep)
  const bind = useSubAgentBind(id, path, agent)
  const nameId = React.useId()
  const name = adkName(agent.name)
  const lookup = useIssueLookup({ whole: true })
  const issues = lookup.of(`agents.${agent.id}.name`)
  const invalid = issues.some((i) => i.level === "error")
  return (
    // Its settings are the sub-agent's: their issues are under agents.<id>.
    <FieldIssuesPrefix prefix={`agents.${agent.id}.`}>
      <Field
        data-invalid={invalid || undefined}
        data-field={`agents.${agent.id}.name`}
      >
        <FieldLabel htmlFor={nameId}>Its name</FieldLabel>
        <Input
          id={nameId}
          aria-invalid={invalid || undefined}
          value={agent.name}
          onChange={(event) =>
            updateStep(
              id,
              (step) =>
                updateSubAgent(step, path, (a) => ({
                  ...a,
                  name: event.target.value,
                })),
              `${path.join("/")}:name`
            )
          }
        />
        <IssueMessages issues={issues} />
        <FieldDescription hidden={issues.length > 0}>
          {name ? (
            <>ADK calls it {code(name)}.</>
          ) : (
            "Its name needs a letter for ADK to call it by."
          )}
          {agent.kind === "llm" && (
            <>
              {" "}
              The agents after it read its answer as{" "}
              {code(`{{ state.${agent.id} }}`)}.
            </>
          )}
        </FieldDescription>
      </Field>
      {agent.kind === "llm" ? (
        <LlmFields
          id={id}
          instruction={subAgentInstruction(agent.id)}
          bind={bind as Bind<LlmSettings>}
          node={false}
          onOpen={onOpen}
        />
      ) : (
        <TeamFields
          bind={bind as Bind<TeamSettings>}
          kind={agent.kind}
          onOpen={onOpen}
        />
      )}
    </FieldIssuesPrefix>
  )
}

/* -------------------------------------------------------------------------- */
/* Kinds                                                                      */
/* -------------------------------------------------------------------------- */

type Props<K extends AgentKind> = {
  id: string
  config: AgentConfigs[K]
  onOpen: (id: string) => void
}

function StartFields({ id, config }: Props<"start">) {
  const { set } = useNodeBind<"start">(id, config)
  return (
    <SchemaField
      issue="input_schema"
      label="Input fields"
      title="What a run starts with"
      description={
        <>
          The values every run starts with: each field, its type and whether
          it&apos;s required. Every node reads them as {code("input")}.
        </>
      }
      schema={config.input_schema}
      onChange={(schema) => set("input_schema", schema)}
      empty="Not declared: a run can start with anything."
      subject={{ one: "input", many: "inputs" }}
    />
  )
}

function SavedFields({ id, config }: Props<"saved">) {
  const { set } = useNodeBind<"saved">(id, config)
  const self = useAgentBuilder((s) => s.meta.id)
  const agents = useAgentBuilder((s) => s.lookups.agents)
  const others = Object.entries(agents).filter(([agentId]) => agentId !== self)
  return (
    <ChoiceField
      issue="agent"
      label="Workflow"
      value={config.agent}
      placeholder={
        others.length ? "Pick a workflow" : "No other workflows yet"
      }
      options={others.map(([value, label]) => ({ value, label }))}
      onChange={(value) => set("agent", value)}
      description="It runs here whole: what comes in is its input, and what it hands on is this node's."
    />
  )
}

function HumanInputFields({ id, config }: Props<"human_input">) {
  const { set } = useNodeBind<"human_input">(id, config)
  return (
    <>
      <Setting
        id={id}
        setting="message"
        label="What they're asked"
        value={config.message}
        multiline
        rows={3}
        placeholder="Send this reply to the customer? {{ previous }}"
        onChange={(value) => set("message", value)}
        description={
          <>
            Type {code("{{")} to put in the run&apos;s data. The run waits here
            until they answer.
          </>
        }
      />
      <SchemaField
        issue="response_schema"
        label="What they answer"
        title="What the person answers"
        description="The fields of their answer and their types. It's checked against them, and it's what this node hands on: the next node reads it as previous."
        schema={config.response_schema}
        onChange={(schema) => set("response_schema", schema)}
        empty="Not declared: they can answer anything."
        subject={{ one: "answer", many: "answers" }}
      />
    </>
  )
}

function LlmNodeFields({ id, config, onOpen }: Props<"llm">) {
  const bind = useNodeBind<"llm">(id, config)
  return (
    <LlmFields
      id={id}
      instruction="instruction"
      bind={bind}
      node
      onOpen={onOpen}
    />
  )
}

function TeamNodeFields({
  id,
  config,
  kind,
  onOpen,
}: Props<TeamKind> & { kind: TeamKind }) {
  const bind = useNodeBind<"loop_agent">(id, config as LoopSettings)
  return <TeamFields bind={bind} kind={kind} onOpen={onOpen} />
}

/** A node's name, and the name ADK calls it by. */
function NodeName({ id, step }: { id: string; step: AgentStep }) {
  const updateStep = useAgentBuilder((s) => s.updateStep)
  const nameId = React.useId()
  const name = adkName(step.name)
  const { issues, invalid, key } = useFieldIssues("name")
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
        <FieldDescription>
          {name ? (
            <>ADK calls it {code(name)}.</>
          ) : (
            "Its name needs a letter for ADK to call it by."
          )}
        </FieldDescription>
      )}
    </Field>
  )
}

/** The way back from a sub-agent's settings: its agent, and the agents above it. */
function Trail({
  step,
  path,
  onGo,
}: {
  step: AgentStep
  path: string[]
  onGo: (path: string[]) => void
}) {
  const crumbs = [
    { name: step.name, path: [] as string[] },
    ...path.map((_, index) => {
      const at = path.slice(0, index + 1)
      return { name: subAgentAt(step, at)?.name ?? "", path: at }
    }),
  ]
  return (
    <nav
      aria-label="Sub-agent"
      className="-mt-1 flex flex-wrap items-center gap-1 text-xs"
    >
      <Button
        type="button"
        variant="ghost"
        size="icon-xs"
        aria-label="Back"
        onClick={() => onGo(path.slice(0, -1))}
        className="text-muted-foreground"
      >
        <Icon icon="left" />
      </Button>
      {crumbs.map((crumb, index) => {
        const last = index === crumbs.length - 1
        return (
          <React.Fragment key={crumb.path.join("/") || "node"}>
            {index > 0 && <span className="text-subtle">›</span>}
            {last ? (
              <span aria-current="page" className="font-medium text-foreground">
                {crumb.name.trim() || "Unnamed"}
              </span>
            ) : (
              <button
                type="button"
                onClick={() => onGo(crumb.path)}
                className="text-muted-foreground hover:text-foreground hover:underline hover:underline-offset-3"
              >
                {crumb.name.trim() || "Unnamed"}
              </button>
            )}
          </React.Fragment>
        )
      })}
    </nav>
  )
}

/** The settings of a node, for its kind; or of the sub-agent opened in it. */
export function AgentFields({ id, step }: { id: string; step: AgentStep }) {
  // The sub-agent open, by the IDs leading to it; none: the node's own.
  const [path, setPath] = React.useState<string[]>([])
  // An issue clicked opens where its setting is: a sub-agent's settings for
  // one of theirs (agents.<id>.…), else the node's own.
  const focusField = useAgentBuilder((s) => s.focusField)
  const [asked, setAsked] = React.useState<string | null>(null)
  if (focusField !== asked) setAsked(focusField)
  if (focusField && focusField !== asked) {
    const sub = /^agents\.([^.]+)\./.exec(focusField)?.[1]
    const found = sub
      ? [...walkSubAgents(subAgentsOf(step))].find((a) => a.agent.id === sub)
      : undefined
    setPath(found ? found.path : [])
  }
  // A sub-agent gone (removed, undone) goes back to the nearest still there.
  let open = path
  while (open.length && !subAgentAt(step, open)) open = open.slice(0, -1)
  if (open !== path) setPath(open)
  const onOpen = (subId: string) => setPath([...open, subId])

  const agent = open.length ? subAgentAt(step, open) : undefined
  if (agent) {
    return (
      <>
        <Trail step={step} path={open} onGo={setPath} />
        <SubAgentFields
          key={agent.id}
          id={id}
          path={open}
          agent={agent}
          onOpen={onOpen}
        />
      </>
    )
  }
  const fields = kindFields(id, step, onOpen)
  if (step.kind === "start") return fields
  return (
    <>
      <NodeName id={id} step={step} />
      {fields}
    </>
  )
}

function kindFields(id: string, step: AgentStep, onOpen: (id: string) => void) {
  if (isForgeKind(step.kind))
    return <StepFields id={id} step={step as StepData} />
  switch (step.kind) {
    case "start":
      return <StartFields id={id} config={step.config} onOpen={onOpen} />
    case "llm":
      return <LlmNodeFields id={id} config={step.config} onOpen={onOpen} />
    case "sequential":
    case "parallel":
    case "loop_agent":
      return (
        <TeamNodeFields
          id={id}
          config={step.config}
          kind={step.kind}
          onOpen={onOpen}
        />
      )
    case "saved":
      return <SavedFields id={id} config={step.config} onOpen={onOpen} />
    case "human_input":
      return <HumanInputFields id={id} config={step.config} onOpen={onOpen} />
  }
}
