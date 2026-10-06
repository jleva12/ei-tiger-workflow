import { VersionField } from "@/features/builder/components/version-field"
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
import { FieldsReadOnly } from "@/features/builder/components/fields/read-only"
import {
  useCompanionCard,
  useOpenCompanion,
  useRevealCompanions,
  useSettingsCard,
} from "@/features/builder/components/settings-companion"
import {
  SettingsCompanion,
  SettingsHint,
} from "@/features/builder/components/settings-dialog"
import type { BuilderIssue } from "@/features/builder/lib/types"
import { Icon } from "@/components/forge/icon"
import type { IconProp } from "@/components/forge/icons"
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
  agentInput,
  subAgentInstruction,
} from "@/features/adk-workflows/lib/fields"
import {
  LLM_TOOL_KINDS,
  newTool,
  TOOL_KINDS,
  toolsAt,
  updateTools,
} from "@/features/adk-workflows/lib/tools"
import {
  ToolFields,
  type ToolBind,
  type ToolKind,
  type ToolLookups,
} from "@/features/agents/components/tool-fields"
import { CHAT_KINDS } from "@/features/agents/lib/model"
import type { ExpressionSetting } from "@/features/steps/lib/fields"
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
  type LlmTool,
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
import { subAgentLine, toolLine } from "./agent-lines"
import { useAgentBuilder } from "./agent-store"

/*
 * The settings of each kind of node, as its settings dialog shows them.
 * Every change goes to the builder's store as it's typed; typing in one
 * field is one step of undo. An agent's sub-agents and tools are listed in
 * its settings; opening one shows its settings in a card beside them, and
 * theirs open beside that, up to the dialog's four cards (past those, in
 * place in the fourth, with the way back above). Forge's steps show the
 * workflow builder's fields
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
  fallback,
  ...field
}: Omit<
  React.ComponentProps<typeof ExpressionField>,
  "mode" | "scope" | "check"
> & {
  id: string
  /** Its key in adk-workflows/lib/fields.ts. */
  setting: string
  /** How it's written while the node doesn't hold it yet (an input not filled in). */
  fallback?: Pick<ExpressionSetting, "mode" | "check">
}) {
  const scopeOf = useAgentBuilder((s) => s.scopeOf)
  const step = useAgentBuilder((s) => s.nodes.find((n) => n.id === id)?.data)
  const scope = React.useMemo(() => scopeOf(id), [scopeOf, id])
  const spec = React.useMemo(
    () =>
      (step ? agentExpressionSetting(step, setting) : undefined) ?? fallback,
    [step, setting, fallback]
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
/* Cards                                                                      */
/* -------------------------------------------------------------------------- */

/** The node's own place: no sub-agent. */
const NODE: string[] = []

/** The card a sub-agent's settings open in, beside the card its row is in. */
const agentCard = (subId: string) => `agent:${subId}`
/** The card a tool's settings open in. */
const toolCard = (toolId: string) => `tool:${toolId}`

/**
 * What a row opens: the sub-agent at `path` (the IDs leading to it); with
 * `tool`, that tool of the agent at `path` (none: the node's).
 */
type Target = { path: string[]; tool?: string }

/**
 * Where rows open what they hold in the fourth card, which nothing can open
 * after: in its place, with the way back above. A sub-agent's card gives it.
 */
const InPlace = React.createContext<((target: Target) => void) | null>(null)

/**
 * Opens what a row holds: its settings in a card beside the card the row
 * is in, or in place in the fourth card.
 */
function useOpenTarget() {
  const openCard = useOpenCompanion()
  const { canOpen } = useSettingsCard()
  const inPlace = React.useContext(InPlace)
  return React.useCallback(
    (target: Target) => {
      if (!canOpen) return inPlace?.(target)
      openCard(
        target.tool
          ? toolCard(target.tool)
          : agentCard(target.path[target.path.length - 1])
      )
    },
    [canOpen, inPlace, openCard]
  )
}

/**
 * Where a setting is, by its key (adk-workflows/lib/fields.ts): the
 * sub-agent it's in (none: the node), and the tool; undefined if that's gone.
 */
function placeOf(step: AgentStep, field: string): Target | undefined {
  const sub = /^agents\.([^.]+)\./.exec(field)?.[1]
  const found = sub
    ? [...walkSubAgents(subAgentsOf(step))].find((a) => a.agent.id === sub)
    : undefined
  if (sub && !found) return undefined
  const path = found?.path ?? NODE
  const tool = /(?:^|\.)tools\.([^.]+)\./.exec(field)?.[1]
  return toolsAt(step, path)?.some((x) => x.id === tool)
    ? { path, tool }
    : { path }
}

/** The cards a setting is in: the sub-agents leading to it, then its tool's. */
function cardsFor(step: AgentStep, field: string): string[] {
  const place = placeOf(step, field)
  if (!place) return []
  return [
    ...place.path.map(agentCard),
    ...(place.tool ? [toolCard(place.tool)] : []),
  ]
}

/** The name of the agent at `path` in the node (none: the node's own). */
function useAgentName(id: string, path: string[]) {
  return useAgentBuilder((s) => {
    const step = s.nodes.find((n) => n.id === id)?.data
    const name = step && (path.length ? subAgentAt(step, path)?.name : step.name)
    return name?.trim() || "Unnamed"
  })
}

/**
 * A sub-agent's or a tool's settings, in a card beside the card its row is
 * in. Like the settings, they change as they're typed: nothing to save.
 */
function ItemCard({
  card,
  title,
  description,
  icon,
  children,
}: {
  card: string
  title: string
  description: string
  icon: IconProp
  children: React.ReactNode
}) {
  const { hide } = useCompanionCard(card)
  const readOnly = React.useContext(FieldsReadOnly)
  return (
    <SettingsCompanion
      id={card}
      title={title}
      description={description}
      icon={icon}
      size="sheet"
      closeLabel={`Close ${title}'s settings`}
      bodyClassName="flex flex-col gap-4 px-5 py-4"
      footer={
        <>
          <SettingsHint>
            {readOnly ? "Read-only" : "Changes apply as you make them"}
          </SettingsHint>
          <Button
            type="button"
            size="sm"
            className="max-[600px]:ml-auto"
            onClick={hide}
          >
            Done
          </Button>
        </>
      }
    >
      {/* Read-only, every native field is disabled with it, as in the settings. */}
      <fieldset disabled={readOnly} className="contents">
        {children}
      </fieldset>
    </SettingsCompanion>
  )
}

/**
 * A row of an agent's list (a sub-agent, a tool): it opens its settings in
 * a card beside, and sums up their issues, since they're in there.
 */
function ItemRow({
  field,
  card,
  glyph,
  name,
  line,
  issues,
  number,
  onOpen,
  children,
}: {
  /** Its key, for an issue to be found by. */
  field: string
  /** The card it opens. */
  card: string
  glyph: React.ReactNode
  name: string
  /** What it is, without issues. */
  line: string
  issues: BuilderIssue[]
  /** Its place, where the order matters. */
  number?: number
  onOpen: () => void
  /** Its actions: moving it, removing it. */
  children: React.ReactNode
}) {
  const { open } = useCompanionCard(card)
  const error = issues.some((i) => i.level === "error")
  return (
    <div
      data-field={field}
      className={cn(
        "flex items-center gap-1 rounded-(--radius-control) border py-1 pr-1 pl-1.5",
        error && "border-destructive"
      )}
    >
      <button
        type="button"
        aria-expanded={open}
        onClick={onOpen}
        className={cn(
          "flex min-w-0 flex-1 items-center gap-2.5 rounded-(--radius-item) px-1 py-0.5 text-left transition-colors duration-150 hover:bg-muted",
          open && "bg-muted"
        )}
      >
        {number !== undefined && (
          <span className="w-3 shrink-0 text-center text-2xs text-subtle tabular-nums">
            {number}
          </span>
        )}
        {glyph}
        <span className="flex min-w-0 flex-1 flex-col">
          <span className="truncate text-xs font-medium text-foreground">
            {name.trim() || "Unnamed"}
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
              {line}
            </span>
          )}
        </span>
        <Icon icon="right" size={14} className="shrink-0 text-subtle" />
      </button>
      {children}
    </div>
  )
}

/* -------------------------------------------------------------------------- */
/* Sub-agents                                                                 */
/* -------------------------------------------------------------------------- */

/**
 * An agent's sub-agents: a row each, opening its settings in a card beside,
 * moved and removed in place; a menu adds one of any kind, and opens it.
 */
function SubAgentList({
  id,
  path,
  label,
  agents,
  ordered,
  onChange,
  description,
  issue,
}: {
  /** The node it's in. */
  id: string
  /** The agent they're the sub-agents of, by the IDs leading to it. */
  path: string[]
  label: string
  agents: SubAgent[]
  /** The order they run in matters: rows can move. */
  ordered: boolean
  onChange: (agents: SubAgent[]) => void
  description?: React.ReactNode
  /** The setting it holds, for its issues. */
  issue?: string
}) {
  const { issues: own, key } = useFieldIssues(issue)
  const nodes = useAgentBuilder((s) => s.nodes)
  const openTarget = useOpenTarget()
  const move = (from: number, to: number) => {
    const next = [...agents]
    const [moved] = next.splice(from, 1)
    next.splice(to, 0, moved)
    onChange(next)
  }
  const add = (kind: SubAgentKind) => {
    const agent = newSubAgent(kind, namesIn(nodes.map((n) => n.data)))
    onChange([...agents, agent])
    openTarget({ path: [...path, agent.id] })
  }
  return (
    <div
      role="group"
      aria-label={label}
      data-field={key}
      className="flex flex-col gap-2"
    >
      <span className="text-xs font-medium text-foreground">{label}</span>
      {agents.map((agent, index) => (
        <SubAgentRow
          key={agent.id}
          id={id}
          parent={path}
          agent={agent}
          number={ordered ? index + 1 : undefined}
        >
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
        </SubAgentRow>
      ))}
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

/** A sub-agent's row, and the card its settings open in. */
function SubAgentRow({
  id,
  parent,
  agent,
  number,
  children,
}: {
  id: string
  /** The agent it's a sub-agent of, by the IDs leading to it. */
  parent: string[]
  agent: SubAgent
  number?: number
  children: React.ReactNode
}) {
  const path = React.useMemo(() => [...parent, agent.id], [parent, agent.id])
  const lookups = useAgentBuilder((s) => s.lookups)
  const owner = useAgentName(id, parent)
  const openTarget = useOpenTarget()
  // Its issues, and its sub-agents': its row says so, since they're in its card.
  const whole = useIssueLookup({ whole: true })
  const issues = [
    agent,
    ...[...walkSubAgents(agent.config.sub_agents)].map((a) => a.agent),
  ]
    .flatMap((a) => whole.under(`agents.${a.id}`))
    .sort((a, b) => (a.level === b.level ? 0 : a.level === "error" ? -1 : 1))
  const info = AGENT_KINDS[agent.kind]
  const card = agentCard(agent.id)
  return (
    <>
      <ItemRow
        field={whole.key(`agents.${agent.id}`)}
        card={card}
        glyph={<KindGlyph info={info} size="sm" />}
        name={agent.name}
        line={`${info.label} · ${subAgentLine(agent, lookups)}`}
        issues={issues}
        number={number}
        onOpen={() => openTarget({ path })}
      >
        {children}
      </ItemRow>
      <ItemCard
        card={card}
        title={agent.name.trim() || "Unnamed"}
        description={`${info.label} · a sub-agent of ${owner}`}
        icon={info.icon}
      >
        <SubAgentCard id={id} path={path} agent={agent} />
      </ItemCard>
    </>
  )
}

/**
 * A sub-agent's card: its settings. In the fourth card, which nothing can
 * open after, what its rows hold opens in its place instead, with the way
 * back above; opened to go to an issue deeper than that, it starts there.
 */
function SubAgentCard({
  id,
  path,
  agent,
}: {
  id: string
  path: string[]
  agent: SubAgent
}) {
  const step = useAgentBuilder((s) => s.nodes.find((n) => n.id === id)?.data)
  const focusField = useAgentBuilder((s) => s.focusField)
  const { canOpen } = useSettingsCard()
  const [at, setAt] = React.useState<Target>(() => {
    const place =
      !canOpen && step && focusField ? placeOf(step, focusField) : undefined
    const below = path.every((sub, index) => place?.path[index] === sub)
    return place && below ? place : { path }
  })
  // What's open in place gone (removed, undone): the nearest still there.
  let open = at.path
  while (open.length > path.length && !(step && subAgentAt(step, open)))
    open = open.slice(0, -1)
  const tool =
    at.tool && step
      ? toolsAt(step, open)?.find((x) => x.id === at.tool)
      : undefined
  if (open !== at.path || (at.tool && !tool)) setAt({ path: open })
  const deeper =
    step && open.length > path.length ? subAgentAt(step, open) : undefined
  const shown = deeper ?? agent
  return (
    <InPlace.Provider value={setAt}>
      {step && (deeper || tool) && (
        <Trail
          step={step}
          root={path}
          path={open}
          tool={tool}
          onGo={(to) => setAt({ path: to })}
        />
      )}
      {tool ? (
        <ToolSettings key={tool.id} id={id} path={open} tool={tool} />
      ) : (
        <SubAgentFields
          key={shown.id}
          id={id}
          path={deeper ? open : path}
          agent={shown}
        />
      )}
    </InPlace.Provider>
  )
}

/**
 * The way back from what's open in place in the fourth card: the card's
 * sub-agent (at `root`), the sub-agents below it, and a tool.
 */
function Trail({
  step,
  root,
  path,
  tool,
  onGo,
}: {
  step: AgentStep
  root: string[]
  path: string[]
  /** A tool open in the agent at `path`. */
  tool?: LlmTool
  onGo: (path: string[]) => void
}) {
  const crumbs = [
    ...path.slice(root.length - 1).map((_, index) => {
      const at = path.slice(0, root.length + index)
      return { name: subAgentAt(step, at)?.name ?? "", path: at }
    }),
    ...(tool ? [{ name: tool.name, path }] : []),
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
        onClick={() => onGo(tool ? path : path.slice(0, -1))}
        className="text-muted-foreground"
      >
        <Icon icon="left" />
      </Button>
      {crumbs.map((crumb, index) => {
        const last = index === crumbs.length - 1
        return (
          <React.Fragment key={`${index}:${crumb.path.join("/")}`}>
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

/* -------------------------------------------------------------------------- */
/* Tools                                                                      */
/* -------------------------------------------------------------------------- */

/** What a person confirming each of a workflow's tool calls means. */
const CONFIRM_IN_RUNS =
  "The run waits for a person to allow each call, on its page, showing what the model wants to send."

/** The organization's agents, workflows, knowledge bases and MCP servers, as tools pick them. */
function useToolLookups(): ToolLookups {
  const lookups = useAgentBuilder((s) => s.lookups)
  return React.useMemo(
    () => ({
      agents: Object.fromEntries(
        Object.entries(lookups.chatAgents).map(([id, a]) => [id, a.name])
      ),
      workflows: lookups.agents,
      workflowVersions: lookups.workflowVersions,
      knowledgeBases: lookups.knowledgeBases,
      mcpServers: lookups.mcpServers,
    }),
    [lookups]
  )
}

/**
 * An LLM agent's tools: a row each, opening its settings in a card beside,
 * removed in place; a menu adds one of the Agents builder's kinds, and
 * opens it.
 */
function ToolList({
  id,
  path,
  tools,
  onChange,
}: {
  /** The node it's in. */
  id: string
  /** The agent they're the tools of, by the IDs leading to it; none: the node. */
  path: string[]
  tools: LlmTool[]
  onChange: (tools: LlmTool[]) => void
}) {
  const { issues: own, key } = useFieldIssues("tools")
  const openTarget = useOpenTarget()
  const add = (kind: (typeof LLM_TOOL_KINDS)[number]) => {
    const tool = newTool(
      kind,
      tools.map((x) => x.name)
    )
    onChange([...tools, tool])
    openTarget({ path, tool: tool.id })
  }
  return (
    <div
      role="group"
      aria-label="Tools"
      data-field={key}
      className="flex flex-col gap-2"
    >
      <span className="text-xs font-medium text-foreground">Tools</span>
      {tools.map((tool) => (
        <ToolRow key={tool.id} id={id} path={path} tool={tool}>
          <Button
            type="button"
            variant="ghost"
            size="icon-xs"
            aria-label={`Remove ${tool.name}`}
            onClick={() => onChange(tools.filter((x) => x.id !== tool.id))}
            className="text-muted-foreground hover:text-destructive"
          >
            <Icon icon={Delete02Icon} />
          </Button>
        </ToolRow>
      ))}
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
          Add a tool
        </DropdownMenuTrigger>
        <DropdownMenuContent align="start" className="w-72">
          <DropdownMenuGroup>
            <DropdownMenuLabel>A tool that calls…</DropdownMenuLabel>
            {LLM_TOOL_KINDS.map((kind) => (
              <DropdownMenuItem
                key={kind}
                onClick={() => add(kind)}
                className="items-start"
              >
                <KindGlyph info={CHAT_KINDS[kind]} size="sm" />
                <span className="flex min-w-0 flex-col">
                  <span>{TOOL_KINDS[kind].label}</span>
                  <span className="text-2xs text-muted-foreground">
                    {TOOL_KINDS[kind].summary}
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
        <p className="text-xs/[1.5] text-muted-foreground">
          What the agent can call: the organization&apos;s MCP servers and
          knowledge bases, HTTP endpoints, OpenAPI specs, agents from the Agents
          page and workflows, set up as on the Agents page.
        </p>
      )}
    </div>
  )
}

/** A tool's row, and the card its settings open in. */
function ToolRow({
  id,
  path,
  tool,
  children,
}: {
  id: string
  /** The agent it's a tool of, by the IDs leading to it; none: the node. */
  path: string[]
  tool: LlmTool
  children: React.ReactNode
}) {
  const lookup = useIssueLookup()
  const lookups = useToolLookups()
  const owner = useAgentName(id, path)
  const openTarget = useOpenTarget()
  const kind = TOOL_KINDS[tool.kind]
  const card = toolCard(tool.id)
  return (
    <>
      <ItemRow
        field={lookup.key(`tools.${tool.id}`)}
        card={card}
        glyph={<KindGlyph info={CHAT_KINDS[tool.kind]} size="sm" />}
        name={tool.name}
        line={`${kind.label} · ${toolLine(tool, lookups)}`}
        issues={lookup.under(`tools.${tool.id}`)}
        onOpen={() => openTarget({ path, tool: tool.id })}
      >
        {children}
      </ItemRow>
      <ItemCard
        card={card}
        title={tool.name.trim() || "Unnamed"}
        description={`${kind.label} · a tool of ${owner}`}
        icon={kind.icon}
      >
        <ToolSettings id={id} path={path} tool={tool} />
      </ItemCard>
    </>
  )
}

/** One of an LLM agent's tools: its name, and its kind's settings. */
function ToolSettings({
  id,
  path,
  tool,
}: {
  id: string
  /** The sub-agent it's in, by the IDs leading to it; none: the node. */
  path: string[]
  tool: LlmTool
}) {
  const updateStep = useAgentBuilder((s) => s.updateStep)
  const self = useAgentBuilder((s) => s.meta.id)
  const lookups = useToolLookups()
  const nameId = React.useId()
  const at = `${path.join("/")}:tools/${tool.id}`
  const prefix = `${path.length ? `agents.${path[path.length - 1]}.` : ""}tools.${tool.id}.`
  const change = React.useCallback(
    (edit: (tool: LlmTool) => LlmTool, key: string) =>
      updateStep(
        id,
        (step) =>
          updateTools(step, path, (tools) =>
            tools.map((x) => (x.id === tool.id ? edit(x) : x))
          ),
        `${at}:${key}`
      ),
    [id, path, tool.id, at, updateStep]
  )
  const bind = React.useMemo<ToolBind<ToolKind>>(() => {
    const patch = (values: object, key: string) =>
      change(
        (x) => ({ ...x, config: { ...x.config, ...values } }) as LlmTool,
        key
      )
    return {
      config: tool.config as ToolBind<ToolKind>["config"],
      patch,
      set: (field, value) => patch({ [field]: value }, String(field)),
    }
  }, [tool.config, change])
  const lookup = useIssueLookup({ whole: true })
  const issues = lookup.of(`${prefix}name`)
  const invalid = issues.some((i) => i.level === "error")
  const name = adkName(tool.name)
  return (
    <FieldIssuesPrefix prefix={prefix} whole>
      <Field data-invalid={invalid || undefined} data-field={`${prefix}name`}>
        <FieldLabel htmlFor={nameId}>Its name</FieldLabel>
        <Input
          id={nameId}
          aria-invalid={invalid || undefined}
          value={tool.name}
          onChange={(event) =>
            change((x) => ({ ...x, name: event.target.value }), "name")
          }
        />
        <IssueMessages issues={issues} />
        <FieldDescription hidden={issues.length > 0}>
          {tool.kind === "http_tool" || tool.kind === "knowledge_base" ? (
            name ? (
              <>The model calls it {code(name)}.</>
            ) : (
              "Its name is the tool's: it needs a letter for the model to call it by."
            )
          ) : (
            "What the agent's settings call it."
          )}
        </FieldDescription>
      </Field>
      <ToolFields
        kind={tool.kind}
        bind={bind}
        lookups={lookups}
        self={self}
        confirmAbout={CONFIRM_IN_RUNS}
      />
    </FieldIssuesPrefix>
  )
}

/* -------------------------------------------------------------------------- */
/* An agent from the Agents page                                              */
/* -------------------------------------------------------------------------- */

const INPUT_EXPRESSION: Pick<ExpressionSetting, "mode" | "check"> = {
  mode: "expression",
  check: {},
}

/**
 * An LLM node that is an agent from the Agents page: which one, which
 * version, what it's sent and its input fields, and what it answers.
 */
function AgentSourceFields({
  id,
  bind,
}: {
  id: string
  bind: Bind<AgentConfigs["llm"]>
}) {
  const { config, set, patch } = bind
  const chatAgents = useAgentBuilder((s) => s.lookups.chatAgents)
  const chosen = config.agent ? chatAgents[config.agent] : undefined
  const options = [
    ...Object.entries(chatAgents).map(([value, a]) => ({
      value,
      label: a.name,
    })),
    // A pick that's gone keeps its place, so the select still shows it.
    ...(config.agent && !chosen
      ? [{ value: config.agent, label: "Deleted agent" }]
      : []),
  ]
  const declared = chosen?.input.properties
  const properties =
    declared && typeof declared === "object"
      ? (declared as Record<string, { description?: string; type?: unknown }>)
      : {}
  const required = new Set(
    Array.isArray(chosen?.input.required)
      ? (chosen.input.required as string[])
      : []
  )
  return (
    <>
      <ChoiceField
        issue="agent"
        label="Agent"
        value={config.agent}
        placeholder={options.length ? "Pick an agent" : "No agents yet"}
        options={options}
        onChange={(value) =>
          patch({ agent: value, version: null, inputs: {} }, "agent")
        }
        description="It runs whole: its own instruction, model, tools and hand-offs, as the Agents page sets it up."
      />
      {chosen && (
        <VersionField
          value={config.version}
          choice={chosen}
          noun="agent"
          onChange={(value) => set("version", value)}
          description="Which of its versions runs: the latest published as each run starts, one that never changes, or its draft as it was when the run started."
        />
      )}
      <Setting
        id={id}
        setting="message"
        label="What it's sent"
        value={config.message}
        multiline
        rows={3}
        placeholder="{{ previous }}"
        onChange={(value) => set("message", value)}
        description={
          <>
            Type {code("{{")} to put in the run&apos;s data. Empty: what the
            node before handed on.
          </>
        }
      />
      {Object.entries(properties).map(([field, schema]) => (
        <Setting
          key={field}
          id={id}
          setting={agentInput(field)}
          fallback={INPUT_EXPRESSION}
          label={`${field}${required.has(field) ? "" : " (optional)"}`}
          value={config.inputs[field] ?? ""}
          placeholder="input.customer_tier"
          onChange={(value) =>
            patch(
              { inputs: { ...config.inputs, [field]: value } },
              agentInput(field)
            )
          }
          description={
            schema.description ||
            "One of its input fields: a JSONata expression over the run's data."
          }
        />
      ))}
      <SchemaField
        issue="output_schema"
        label="What it answers"
        title="The JSON the agent answers with"
        description="Its fields and their types, when it answers in JSON: later nodes read them. Not declared: its answer is text."
        schema={config.output_schema}
        onChange={(schema) => set("output_schema", schema)}
        empty="Not declared: it answers in text."
        subject={{ one: "answer", many: "answers" }}
      />
    </>
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
  path,
}: {
  /** The node it's in (or is). */
  id: string
  /** Its instruction's key among the node's settings (adk-workflows/lib/fields.ts). */
  instruction: string
  bind: Bind<LlmSettings & Partial<Pick<AgentConfigs["llm"], "mode">>>
  /** It's a node of the graph, not a sub-agent: it has a mode. */
  node: boolean
  /** Where it is: the IDs leading to it, as a sub-agent; none: the node. */
  path: string[]
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
      <ToolList
        id={id}
        path={path}
        tools={config.tools}
        onChange={(tools) => set("tools", tools)}
      />
      <SubAgentList
        id={id}
        path={path}
        issue="sub_agents"
        label="Hands off to"
        agents={config.sub_agents}
        ordered={false}
        onChange={(agents) => set("sub_agents", agents)}
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
  id,
  path,
  bind,
  kind,
}: {
  /** The node it's in (or is). */
  id: string
  /** Where it is: the IDs leading to it, as a sub-agent; none: the node. */
  path: string[]
  bind: Bind<TeamSettings & Partial<Pick<LoopSettings, "max_iterations">>>
  kind: TeamKind
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
        id={id}
        path={path}
        issue="sub_agents"
        label={TEAM_WORDS[kind].list}
        agents={config.sub_agents}
        ordered={kind !== "parallel"}
        onChange={(agents) => set("sub_agents", agents)}
        description={TEAM_WORDS[kind].about}
      />
    </>
  )
}

/** A sub-agent's settings, in the card its row in its agent's list opens. */
function SubAgentFields({
  id,
  path,
  agent,
}: {
  id: string
  path: string[]
  agent: SubAgent
}) {
  const updateStep = useAgentBuilder((s) => s.updateStep)
  const bind = useSubAgentBind(id, path, agent)
  const nameId = React.useId()
  const name = adkName(agent.name)
  const lookup = useIssueLookup({ whole: true })
  const issues = lookup.of(`agents.${agent.id}.name`)
  const invalid = issues.some((i) => i.level === "error")
  return (
    // Its settings are the sub-agent's: their issues are under agents.<id>,
    // whichever card it's opened from.
    <FieldIssuesPrefix prefix={`agents.${agent.id}.`} whole>
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
          path={path}
        />
      ) : (
        <TeamFields
          id={id}
          path={path}
          bind={bind as Bind<TeamSettings>}
          kind={agent.kind}
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
  const { set, patch } = useNodeBind<"saved">(id, config)
  const self = useAgentBuilder((s) => s.meta.id)
  const agents = useAgentBuilder((s) => s.lookups.agents)
  const versions = useAgentBuilder((s) => s.lookups.workflowVersions)
  const others = Object.entries(agents).filter(([agentId]) => agentId !== self)
  const chosen = config.agent ? versions[config.agent] : undefined
  return (
    <>
      <ChoiceField
        issue="agent"
        label="Workflow"
        value={config.agent}
        placeholder={others.length ? "Pick a workflow" : "No other workflows yet"}
        options={others.map(([value, label]) => ({ value, label }))}
        onChange={(value) => patch({ agent: value, version: null }, "agent")}
        description="It runs here whole: what comes in is its input, and what it hands on is this node's."
      />
      {chosen && (
        <VersionField
          value={config.version ?? null}
          choice={chosen}
          noun="workflow"
          onChange={(value) => set("version", value)}
        />
      )}
    </>
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

function LlmNodeFields({ id, config }: Props<"llm">) {
  const bind = useNodeBind<"llm">(id, config)
  return (
    <>
      <ChoiceField
        issue="source"
        label="The agent"
        value={config.source}
        options={[
          { value: "inline", label: "Set up here" },
          { value: "agent", label: "An agent from the Agents page" },
        ]}
        onChange={(value) => bind.set("source", value)}
        description={
          config.source === "agent"
            ? "One of the organization's agents, used whole: change it on the Agents page, and every workflow that uses it changes with it."
            : "Its instruction, model and tools are set up here, for this workflow alone."
        }
      />
      {config.source === "agent" ? (
        <AgentSourceFields id={id} bind={bind} />
      ) : (
        <LlmFields
          id={id}
          instruction="instruction"
          bind={bind}
          node
          path={NODE}
        />
      )}
    </>
  )
}

function TeamNodeFields({
  id,
  config,
  kind,
}: Props<TeamKind> & { kind: TeamKind }) {
  const bind = useNodeBind<"loop_agent">(id, config as LoopSettings)
  return <TeamFields id={id} path={NODE} bind={bind} kind={kind} />
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

/**
 * The settings of a node, for its kind. Its sub-agents' and tools' open in
 * cards beside them, and theirs beside those. An issue clicked opens the
 * cards its setting is in, or closes them for one of the node's own.
 */
export function AgentFields({ id, step }: { id: string; step: AgentStep }) {
  const reveal = useRevealCompanions()
  const focusField = useAgentBuilder((s) => s.focusField)
  React.useEffect(() => {
    if (focusField) reveal(cardsFor(step, focusField))
  }, [focusField, step, reveal])

  const fields = kindFields(id, step)
  if (step.kind === "start") return fields
  return (
    <>
      <NodeName id={id} step={step} />
      {fields}
    </>
  )
}

function kindFields(id: string, step: AgentStep) {
  if (isForgeKind(step.kind))
    return <StepFields id={id} step={step as StepData} />
  switch (step.kind) {
    case "start":
      return <StartFields id={id} config={step.config} />
    case "llm":
      return <LlmNodeFields id={id} config={step.config} />
    case "sequential":
    case "parallel":
    case "loop_agent":
      return <TeamNodeFields id={id} config={step.config} kind={step.kind} />
    case "saved":
      return <SavedFields id={id} config={step.config} />
    case "human_input":
      return <HumanInputFields id={id} config={step.config} />
  }
}
