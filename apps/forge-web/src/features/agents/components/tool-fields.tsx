import * as React from "react"
import { Delete02Icon } from "@hugeicons/core-free-icons"

import {
  VersionField,
  type VersionChoice,
} from "@/features/builder/components/version-field"
import {
  CheckField,
  ChoiceField,
  CODE,
  OptionalNumberField,
  TextField,
} from "@/features/builder/components/fields/basic-fields"
import { IssueMessages } from "@/features/builder/components/fields/field-issues"
import { useFieldIssues } from "@/features/builder/components/fields/field-issues-context"
import { SchemaField } from "@/features/builder/components/fields/schema-field"
import {
  ExpressionField,
  type TemplateArgument,
} from "@/features/builder/components/fields/expression-field"
import { Icon } from "@/components/forge/icon"
import {
  RICH_ANSWER_COMPONENTS,
  RICH_ANSWER_GROUPS,
  RICH_ANSWER_REQUIRES,
  RICH_ANSWER_VOCABULARY,
  schemaCost,
  withRequired,
  type RichAnswerComponent,
} from "@/components/forge/generative-ui/catalog"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Field, FieldDescription, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { useServiceInfo } from "@/lib/service-info"
import type { ChatConfigs } from "@/features/agents/lib/model"
import {
  editedParameters,
  takesBody,
} from "@/features/agents/lib/http-template"
import { CredentialField } from "@/features/credentials/components/credential-field"
import type { CredentialLookup } from "@/features/credentials/lib/display"
import { OpenApiOperations } from "./openapi-operations"
import { HTTP_METHODS, uid, type HeaderRow } from "@/features/steps/lib/model"
import type { Scope } from "@/features/steps/lib/scope"
import type {
  KnowledgeBaseLookup,
  McpServerLookup,
  SkillLookup,
} from "./chat-agent-store"

/*
 * The settings of each kind of tool, as the Agents builder's settings dialog
 * shows a tool node's, and a workflow's LLM agent shows one of its tools:
 * they're given the tool's settings and how to change them, and the
 * organization's agents, workflows, knowledge bases and MCP servers to pick
 * from. Issues are found by field, under the prefix their dialog sets.
 */

const code = (text: string) => (
  <code className="font-mono text-[0.9em]">{text}</code>
)

/** The kinds of tool these fields set up. */
export type ToolKind =
  | "saved_agent"
  | "adk_workflow"
  | "memory"
  | "knowledge_base"
  | "http_tool"
  | "openapi"
  | "mcp"
  | "code_execution"
  | "skills"
  | "rich_answers"

/** What a tool may pick from: the organization's, by ID. */
export type ToolLookups = {
  /** Agents from the Agents page. */
  agents: Record<string, string>
  workflows: Record<string, string>
  /** Where each workflow is between draft and published, for its version picker. */
  workflowVersions: Record<string, VersionChoice>
  knowledgeBases: Record<string, KnowledgeBaseLookup>
  mcpServers: Record<string, McpServerLookup>
  /** API credentials: what HTTP and OpenAPI tools sign in with. */
  credentials: Record<string, CredentialLookup>
  /** Skills from the organization's skills vault. */
  skills: Record<string, SkillLookup>
}

/** A tool's settings, and how to change them: each change one step of undo by its key. */
export type ToolBind<K extends ToolKind> = {
  config: ChatConfigs[K]
  set: <F extends keyof ChatConfigs[K]>(
    field: F,
    value: ChatConfigs[K][F]
  ) => void
  /** Several settings at once, one step of undo under `key`. */
  patch: (values: Partial<ChatConfigs[K]>, key: string) => void
}

type ToolProps<K extends ToolKind> = ToolBind<K> & {
  lookups: ToolLookups
  /** The organization it's in: whose credentials and specs it reads. */
  organizationId: string
  /** The agent or workflow it's in, which it can't call. */
  self?: string
  /** What a person confirming each call means where it runs. */
  confirmAbout: string
  /**
   * What showing a tool's calls to the people its agent talks with means
   * where it runs; where none is given (a workflow's LLM agent), the setting
   * isn't offered.
   */
  showToPersonAbout?: string
  /** What `{{ }}` in an HTTP tool's URL and body reads where it runs. */
  scope: Scope
  /** That in words: `{{ }}` puts in …. */
  valuesAbout: React.ReactNode
}

function SavedAgentFields({
  config,
  set,
  lookups,
  self,
}: ToolProps<"saved_agent">) {
  const others = Object.entries(lookups.agents).filter(
    ([agentId]) => agentId !== self
  )
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

function WorkflowFields({
  config,
  set,
  patch,
  lookups,
  self,
}: ToolProps<"adk_workflow">) {
  const options = Object.entries(lookups.workflows)
    .filter(([value]) => value !== self)
    .map(([value, label]) => ({
      value,
      label,
    }))
  const chosen = config.workflow
    ? lookups.workflowVersions[config.workflow]
    : undefined
  return (
    <>
      <ChoiceField
        issue="workflow"
        label="Workflow"
        value={config.workflow}
        placeholder={options.length ? "Pick a workflow" : "No workflows yet"}
        options={options}
        onChange={(value) =>
          patch({ workflow: value, version: null }, "workflow")
        }
        description="The agent runs it with its start's input fields and reads its result. Its description tells the agent when to."
      />
      {chosen && (
        <VersionField
          value={config.version ?? null}
          choice={chosen}
          noun="workflow"
          onChange={(value) => set("version", value)}
        />
      )}
      <WorkflowWaitField
        value={config.wait_seconds ?? null}
        onChange={(value) => set("wait_seconds", value ?? WORKFLOW_WAIT)}
      />
    </>
  )
}

/** How long a Workflow tool waits for its run when its node doesn't say. */
const WORKFLOW_WAIT = 120

/** Seconds as a person reads them: 90 s, 2 min, 1 min 30 s. */
function duration(seconds: number) {
  const minutes = Math.floor(seconds / 60)
  const rest = Math.round(seconds % 60)
  if (!minutes) return `${rest} s`
  return rest ? `${minutes} min ${rest} s` : `${minutes} min`
}

/**
 * How long the agent waits for the run before it's told the run is still
 * going, with its ID: at most the deployment's cap, which the admin API says.
 */
function WorkflowWaitField({
  value,
  onChange,
}: {
  value: number | null
  onChange: (value: number | null) => void
}) {
  const most = useServiceInfo().data?.agent_workflow_wait_max
  const waits = value ?? WORKFLOW_WAIT
  return (
    <OptionalNumberField
      issue="wait_seconds"
      label="Wait up to (seconds)"
      value={value}
      min={1}
      max={most}
      integer
      placeholder={String(WORKFLOW_WAIT)}
      onChange={onChange}
      description={
        <>
          The agent waits {duration(waits)} for the run to finish. If it
          hasn&apos;t by then, the agent is told it&apos;s still running, with
          its run&apos;s ID, and the run carries on.
          {most !== undefined &&
            ` This deployment waits at most ${duration(most)}.`}
        </>
      }
    />
  )
}

function MemoryFields({ config, set }: ToolProps<"memory">) {
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

/** Whether the people an agent talks with see a tool's calls in full; offered only where `about` says what that means. */
function ShowToPersonField({
  checked,
  onChange,
  about,
}: {
  checked: boolean
  onChange: (on: boolean) => void
  about?: string
}) {
  if (about === undefined) return null
  return (
    <CheckField
      label="Show its calls in the chat"
      checked={checked}
      onChange={onChange}
      description={about}
    />
  )
}

function CodeExecutionFields({
  config,
  set,
  confirmAbout,
  showToPersonAbout,
}: ToolProps<"code_execution">) {
  return (
    <>
      <p className="text-xs/[1.5] text-muted-foreground">
        The model writes Python and runs it, then reads what it printed and its
        errors. Each run starts afresh, where the deployment&apos;s sandbox mode
        says: in a new OpenShell sandbox for each run, or in local development
        on the server&apos;s own machine, unsandboxed. Where it&apos;s off, the
        agent doesn&apos;t run.
      </p>
      <CheckField
        label="A person confirms each run"
        checked={config.confirm}
        onChange={(on) => set("confirm", on)}
        description={confirmAbout}
      />
      <ShowToPersonField
        checked={config.show_to_person}
        onChange={(on) => set("show_to_person", on)}
        about={showToPersonAbout}
      />
    </>
  )
}

/** A component's description, short: its first sentence (two, if that's a few words), without markup. */
const firstSentence = (text: string) =>
  (
    (/^.{0,40}?[.!?]\s+.*?[.!?](?=\s+[A-Z`]|$)/.exec(text) ??
      /^.*?[.!?](?=\s+[A-Z`]|$)/.exec(text))?.[0] ?? text
  ).replace(/`/g, "")

/** The picks without `name`, and without what can't do without it. */
function withoutComponent(picked: readonly string[], name: string) {
  let left = picked.filter((each) => each !== name)
  for (;;) {
    const kept = left.filter((each) =>
      (RICH_ANSWER_REQUIRES[each as RichAnswerComponent] ?? []).every(
        (needed) => left.includes(needed)
      )
    )
    if (kept.length === left.length) return kept
    left = kept
  }
}

function RichAnswersFields({ config, set }: ToolProps<"rich_answers">) {
  const groupId = React.useId()
  const { issues, key } = useFieldIssues("components")
  const picked = config.components
  const cost = schemaCost(picked)
  const pick = (names: readonly string[], on: boolean) =>
    set(
      "components",
      on
        ? withRequired([...picked, ...names])
        : names.reduce<string[]>(withoutComponent, [...picked])
    )
  return (
    <>
      <p className="text-xs/[1.5] text-muted-foreground">
        The agent may answer with a composition the chat draws beside its text,
        of the components picked here. It calls{" "}
        <code className="font-mono text-2xs">present</code> with it, and is told
        to write a sentence about it too, since callers of the agent&apos;s API
        and A2A get only the call. What the person clicks or fills in comes back
        as their next message.
      </p>
      <div
        role="group"
        aria-labelledby={`${groupId}-label`}
        data-field={key}
        className="flex flex-col gap-2"
      >
        <span
          id={`${groupId}-label`}
          className="flex items-baseline justify-between gap-2 text-xs font-medium text-foreground"
        >
          Components it may use
          <span className="font-normal text-muted-foreground tabular-nums">
            {picked.length} of {RICH_ANSWER_COMPONENTS.length} picked
          </span>
        </span>
        <div
          className="flex max-h-96 flex-col gap-3 overflow-y-auto rounded-(--radius-item) border p-2.5"
          tabIndex={0}
        >
          {RICH_ANSWER_GROUPS.map((group) => {
            const on = group.components.filter((name) => picked.includes(name))
            const all = on.length === group.components.length
            return (
              <div key={group.id} className="flex flex-col gap-1">
                <div className="flex items-center justify-between gap-2">
                  <span className="text-2xs font-medium tracking-wide text-muted-foreground uppercase">
                    {group.label}
                    <span className="ml-1.5 font-normal tracking-normal normal-case tabular-nums">
                      {on.length} of {group.components.length}
                    </span>
                  </span>
                  <Button
                    type="button"
                    variant="ghost"
                    size="xs"
                    aria-label={`${all ? "Untick" : "Tick"} every ${group.label.toLowerCase()} component`}
                    onClick={() => pick(group.components, !all)}
                  >
                    {all ? "None" : "All"}
                  </Button>
                </div>
                {group.components.map((name) => {
                  const needs = RICH_ANSWER_REQUIRES[name]
                  return (
                    <Field
                      key={name}
                      orientation="horizontal"
                      className="items-start"
                    >
                      <Checkbox
                        id={`${groupId}-${name}`}
                        className="mt-0.5"
                        checked={picked.includes(name)}
                        onCheckedChange={(checked) =>
                          pick([name], Boolean(checked))
                        }
                      />
                      <FieldLabel
                        htmlFor={`${groupId}-${name}`}
                        className="flex min-w-0 flex-1 flex-col items-start gap-0.5 font-normal"
                      >
                        <span className="font-mono text-xs">{name}</span>
                        <span className="line-clamp-2 text-2xs text-muted-foreground">
                          {firstSentence(
                            RICH_ANSWER_VOCABULARY[name].description
                          )}
                          {needs && ` Comes with ${needs.join(" and ")}.`}
                        </span>
                      </FieldLabel>
                    </Field>
                  )
                })}
              </div>
            )
          })}
        </div>
        <FieldDescription>
          {picked.length ? (
            <>
              Adds about {cost.openai.toLocaleString()} tokens to every model
              call it makes on OpenAI, and about {cost.gemini.toLocaleString()}{" "}
              on Gemini, which writes the components out in full. Pick only what
              its answers need.
            </>
          ) : (
            "Pick at least one component."
          )}
        </FieldDescription>
        <IssueMessages issues={issues} />
      </div>
    </>
  )
}

function SkillsFields({ config, set, lookups }: ToolProps<"skills">) {
  const known = lookups.skills
  const groupId = React.useId()
  const { issues, key } = useFieldIssues("skills")
  const [filter, setFilter] = React.useState("")
  // A pick that's gone keeps its place, so it can be unticked.
  const gone = config.skills.filter((id) => !known[id])
  const toggle = (id: string, on: boolean) =>
    set(
      "skills",
      on
        ? [...config.skills.filter((x) => x !== id), id]
        : config.skills.filter((x) => x !== id)
    )
  const words = filter.trim().toLowerCase()
  const entries = Object.entries(known)
    .filter(
      ([, skill]) =>
        !words ||
        skill.name.includes(words) ||
        skill.description.toLowerCase().includes(words)
    )
    .sort(([, a], [, b]) => a.name.localeCompare(b.name))
  const total = Object.keys(known).length
  return (
    <>
      <div
        role="group"
        aria-labelledby={`${groupId}-label`}
        data-field={key}
        className="flex flex-col gap-2"
      >
        <span
          id={`${groupId}-label`}
          className="flex items-baseline justify-between gap-2 text-xs font-medium text-foreground"
        >
          Skills it has
          <span className="font-normal text-muted-foreground tabular-nums">
            {config.skills.length} of {total} picked
          </span>
        </span>
        {total > 6 && (
          <Input
            aria-label="Find a skill"
            placeholder="Find a skill"
            value={filter}
            onChange={(event) => setFilter(event.target.value)}
          />
        )}
        {entries.length + gone.length > 0 ? (
          <div
            className="flex max-h-72 flex-col gap-1 overflow-y-auto rounded-(--radius-item) border p-2.5"
            tabIndex={0}
          >
            {entries.map(([id, skill]) => (
              <Field key={id} orientation="horizontal" className="items-start">
                <Checkbox
                  id={`${groupId}-${id}`}
                  className="mt-0.5"
                  checked={config.skills.includes(id)}
                  onCheckedChange={(on) => toggle(id, Boolean(on))}
                />
                <FieldLabel
                  htmlFor={`${groupId}-${id}`}
                  className="flex min-w-0 flex-1 flex-col items-start gap-0.5 font-normal"
                >
                  <span className="font-mono text-xs">{skill.name}</span>
                  <span className="line-clamp-2 text-2xs text-muted-foreground">
                    {skill.description}
                  </span>
                </FieldLabel>
              </Field>
            ))}
            {gone.map((id) => (
              <Field key={id} orientation="horizontal" className="items-center">
                <Checkbox
                  id={`${groupId}-${id}`}
                  checked
                  onCheckedChange={(on) => toggle(id, Boolean(on))}
                />
                <FieldLabel
                  htmlFor={`${groupId}-${id}`}
                  className="font-normal text-muted-foreground"
                >
                  Deleted skill
                </FieldLabel>
              </Field>
            ))}
          </div>
        ) : (
          <FieldDescription>
            {total
              ? "No skill matches."
              : "The organization has no skills yet: add them on the Skills vault page (write, upload, import from GitHub, or the AI skill creator)."}
          </FieldDescription>
        )}
        <FieldDescription>
          The agent is told each skill&apos;s name and description, and loads
          its instructions ({code("load_skill")}) and files (
          {code("load_skill_resource")}) when a task matches. Skills are read as
          the vault has them: a change reaches agents from their next turn, and
          workflows from their next run.
        </FieldDescription>
        <IssueMessages issues={issues} />
      </div>
      <CheckField
        label="It may run the skills' scripts"
        checked={config.run_scripts}
        onChange={(on) => set("run_scripts", on)}
        description="Scripts (scripts/…) run where the deployment's sandbox mode says: in a new OpenShell sandbox, or in local development on the server's own machine, unsandboxed. Otherwise the agent reads a script and follows it itself."
      />
    </>
  )
}

function KnowledgeBaseFields({
  config,
  set,
  lookups,
}: ToolProps<"knowledge_base">) {
  const known = lookups.knowledgeBases
  const groupId = React.useId()
  const { issues, key } = useFieldIssues("knowledge_bases")
  // A pick that's gone keeps its place, so it can be unticked.
  const gone = config.knowledge_bases.filter((kb) => !known[kb])
  const toggle = (kb: string, on: boolean) =>
    set(
      "knowledge_bases",
      on
        ? [...config.knowledge_bases.filter((x) => x !== kb), kb]
        : config.knowledge_bases.filter((x) => x !== kb)
    )
  const entries = Object.entries(known)
  return (
    <>
      <div
        role="group"
        aria-labelledby={`${groupId}-label`}
        data-field={key}
        className="flex flex-col gap-2"
      >
        <span
          id={`${groupId}-label`}
          className="text-xs font-medium text-foreground"
        >
          Knowledge bases it searches
        </span>
        {entries.length + gone.length > 0 ? (
          <div
            className="flex max-h-56 flex-col gap-1.5 overflow-y-auto rounded-(--radius-item) border p-2.5"
            tabIndex={0}
          >
            {entries.map(([kb, found]) => (
              <Field key={kb} orientation="horizontal" className="items-center">
                <Checkbox
                  id={`${groupId}-${kb}`}
                  checked={config.knowledge_bases.includes(kb)}
                  onCheckedChange={(on) => toggle(kb, Boolean(on))}
                />
                <FieldLabel
                  htmlFor={`${groupId}-${kb}`}
                  className="flex min-w-0 flex-1 items-baseline justify-between gap-2 font-normal"
                >
                  <span className="truncate">{found.name}</span>
                  <span className="shrink-0 text-xs text-muted-foreground tabular-nums">
                    {found.kind === "system"
                      ? `${found.ready} of ${found.documents} ${found.documents === 1 ? "repository" : "repositories"} ingested`
                      : `${found.ready} of ${found.documents} searchable`}
                  </span>
                </FieldLabel>
              </Field>
            ))}
            {gone.map((kb) => (
              <Field key={kb} orientation="horizontal" className="items-center">
                <Checkbox
                  id={`${groupId}-${kb}`}
                  checked
                  onCheckedChange={(on) => toggle(kb, Boolean(on))}
                />
                <FieldLabel
                  htmlFor={`${groupId}-${kb}`}
                  className="font-normal text-muted-foreground"
                >
                  Deleted knowledge base
                </FieldLabel>
              </Field>
            ))}
          </div>
        ) : (
          <FieldDescription>
            The organization has no knowledge bases yet: make one and upload its
            documents on the Knowledge bases page.
          </FieldDescription>
        )}
        <FieldDescription>
          The agent searches their documents (parsed, chunked and embedded when
          uploaded), or a system design knowledge base's code (each declaration
          of its repositories' code graphs), by meaning and by their words, and
          reads the passages that match best.
        </FieldDescription>
        <IssueMessages issues={issues} />
      </div>
      <TextField
        issue="description"
        label="What it's for"
        value={config.description}
        multiline
        rows={3}
        placeholder="Our HR policies: leave, benefits, expenses and travel."
        onChange={(value) => set("description", value)}
        description="The model reads this to decide when to search. Empty: it's told the knowledge bases' names and descriptions."
      />
      <OptionalNumberField
        issue="max_results"
        label="Passages a search answers"
        value={config.max_results}
        min={1}
        max={20}
        placeholder="5"
        integer
        onChange={(value) => set("max_results", value ?? 5)}
      />
    </>
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
      <FieldDescription>
        Sent as they are, and shown to everyone who can open it: put keys and
        tokens in a credential.
      </FieldDescription>
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

/**
 * Query parameters: a row each, its name and value side by side and a bin
 * to remove it. Both are templates like the URL, completed as it is; filled
 * in, each is percent-encoded as it's sent.
 */
function QueryRows({
  query,
  onChange,
  scope,
  args,
}: {
  query: HeaderRow[]
  onChange: (query: HeaderRow[]) => void
  scope: Scope
  args: readonly TemplateArgument[]
}) {
  const [added, setAdded] = React.useState<string>()
  const { issues, key } = useFieldIssues("query")
  const change = (row: HeaderRow, patch: Partial<HeaderRow>) =>
    onChange(query.map((x) => (x.id === row.id ? { ...x, ...patch } : x)))
  // No space under a template until it has something to say.
  const quiet = "gap-0 has-[[aria-live]>*]:gap-1.5"
  return (
    <div
      role="group"
      aria-label="Query parameters"
      data-field={key}
      className="flex flex-col gap-2"
    >
      <span className="text-xs font-medium text-foreground">
        Query parameters
      </span>
      <FieldDescription>
        Added to the URL&apos;s query, each name and value encoded as it&apos;s
        sent. A value that&apos;s one placeholder ({code("{status}")}) is left
        out when it comes to nothing; a list is sent once per item.
      </FieldDescription>
      {query.map((row) => (
        <div
          key={row.id}
          className="grid grid-cols-[minmax(0,2fr)_minmax(0,3fr)_auto] items-start gap-1.5"
        >
          <ExpressionField
            mode="template"
            label="Query parameter name"
            labelHidden
            value={row.name}
            placeholder="Key"
            scope={scope}
            check={{ textual: true }}
            args={args}
            autoFocus={row.id === added}
            field={`${key}.${row.id}.name`}
            onChange={(name) => change(row, { name })}
            className={quiet}
          />
          <ExpressionField
            mode="template"
            label={`${row.name.trim() || "Query parameter"} value`}
            labelHidden
            value={row.value}
            placeholder="Value"
            scope={scope}
            check={{ textual: true }}
            args={args}
            field={`${key}.${row.id}`}
            onChange={(value) => change(row, { value })}
            className={quiet}
          />
          <Button
            type="button"
            variant="ghost"
            size="icon"
            aria-label={`Remove ${row.name.trim() || "query parameter"}`}
            onClick={() => onChange(query.filter((x) => x.id !== row.id))}
            className="h-[2.125rem] text-muted-foreground hover:text-destructive"
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
          const row = { id: uid("query"), name: "", value: "" }
          setAdded(row.id)
          onChange([...query, row])
        }}
        className="w-full border-dashed text-muted-foreground"
      >
        <Icon icon="plus" data-icon="inline-start" />
        Add query parameter
      </Button>
      <IssueMessages issues={issues} />
    </div>
  )
}

const CONFIRM = "A person confirms each call"

/** One of an HTTP tool's templates: its URL or its body. */
function TemplateSetting({
  setting,
  ...field
}: Omit<React.ComponentProps<typeof ExpressionField>, "mode" | "field"> & {
  setting: "url" | "body"
}) {
  // Its issues, but for its template's: it checks those itself.
  const { issues, key } = useFieldIssues(setting, { whole: true })
  return (
    <ExpressionField
      {...field}
      mode="template"
      issues={issues.filter((i) => !i.id.includes(":field:"))}
      field={key}
    />
  )
}

const typeOf = (schema: unknown): string | undefined => {
  const type = (schema as { type?: unknown } | undefined)?.type
  return typeof type === "string" ? type : undefined
}

function HttpToolFields({
  config,
  set,
  lookups,
  organizationId,
  confirmAbout,
  showToPersonAbout,
  scope,
  valuesAbout,
}: ToolProps<"http_tool">) {
  // The arguments its URL and body name, with what's declared of each.
  const parameters = React.useMemo(() => editedParameters(config), [config])
  const args = React.useMemo<TemplateArgument[]>(() => {
    const properties = (parameters.properties ?? {}) as Record<
      string,
      { description?: unknown }
    >
    return Object.entries(properties).map(([name, schema]) => ({
      name,
      type: typeOf(schema),
      description:
        typeof schema.description === "string" ? schema.description : undefined,
    }))
  }, [parameters])
  const body = takesBody(config.method)
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
        <TemplateSetting
          setting="url"
          label="URL"
          value={config.url}
          scope={scope}
          check={{ textual: true }}
          args={args}
          placeholder="https://api.example.com/orders/{order_id}?status={status}"
          onChange={(value) => set("url", value)}
        />
      </div>
      <p className="-mt-1 text-xs/[1.6] text-muted-foreground">
        {code("{name}")} puts in an argument the model fills in; {code("{{ }}")}{" "}
        puts in {valuesAbout}. List query parameters below rather than in the
        URL: their values are encoded for you.
      </p>
      <QueryRows
        query={config.query}
        onChange={(value) => set("query", value)}
        scope={scope}
        args={args}
      />
      <CredentialField
        organizationId={organizationId}
        value={config.credential}
        onChange={(value) => set("credential", value)}
        credentials={lookups.credentials}
        url={config.url}
      />
      <HeaderRows
        headers={config.headers}
        onChange={(value) => set("headers", value)}
      />
      {body && (
        <TemplateSetting
          setting="body"
          label="Body"
          value={config.body}
          scope={scope}
          args={args}
          multiline
          rows={5}
          placeholder={
            '{\n  "order_id": {order_id},\n  "note": "Deliver to {name}"\n}'
          }
          onChange={(value) => set("body", value)}
          description={
            <>
              JSON, sent as the request&apos;s body. A placeholder that&apos;s a
              whole value goes in as it is ({code("{count}")} sends {code("2")},
              not {code('"2"')}), and its key is left out when the model
              doesn&apos;t send it; one inside a string is written in.
            </>
          }
        />
      )}
      <SchemaField
        issue="parameters"
        label="What the model sends"
        title="The arguments the model fills in"
        description={
          <>
            The arguments its URL{body ? " and body" : ""} name, as{" "}
            {code("{name}")}: give each its type and say what it is. Only these
            are sent.
          </>
        }
        schema={parameters}
        onChange={(schema) => set("parameters", schema)}
        empty={`None yet: put {name} in its URL${body ? " or body" : ""} for the model to fill in.`}
        subject={{ one: "argument", many: "arguments" }}
      />
      <CheckField
        label={CONFIRM}
        checked={config.confirm}
        onChange={(on) => set("confirm", on)}
        description={confirmAbout}
      />
      <ShowToPersonField
        checked={config.show_to_person}
        onChange={(on) => set("show_to_person", on)}
        about={showToPersonAbout}
      />
    </>
  )
}

function OpenApiFields({
  config,
  set,
  lookups,
  organizationId,
  confirmAbout,
  showToPersonAbout,
}: ToolProps<"openapi">) {
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
      <CredentialField
        organizationId={organizationId}
        value={config.credential}
        onChange={(value) => set("credential", value)}
        credentials={lookups.credentials}
        url={config.source === "url" ? config.url : ""}
      />
      <OpenApiOperations
        organizationId={organizationId}
        source={{
          source: config.source,
          url: config.url,
          spec: config.spec,
          credential: config.credential,
        }}
        value={config.operations}
        onChange={(value) => set("operations", value)}
      />
      <CheckField
        label={CONFIRM}
        checked={config.confirm}
        onChange={(on) => set("confirm", on)}
        description={confirmAbout}
      />
      <ShowToPersonField
        checked={config.show_to_person}
        onChange={(on) => set("show_to_person", on)}
        about={showToPersonAbout}
      />
    </>
  )
}

// The server choice for one set up here, by URL.
const BY_URL = "url"

function McpFields({
  config,
  set,
  patch,
  lookups,
  confirmAbout,
  showToPersonAbout,
}: ToolProps<"mcp">) {
  const servers = lookups.mcpServers
  const server = config.server ? servers[config.server] : undefined
  const options = [
    ...Object.entries(servers).map(([value, s]) => ({ value, label: s.name })),
    // A pick that's gone keeps its place, so the select still shows it.
    ...(config.server && !server
      ? [{ value: config.server, label: "Deleted MCP server" }]
      : []),
    { value: BY_URL, label: "Another server, by URL" },
  ]
  return (
    <>
      <ChoiceField
        issue="server"
        label="Server"
        value={config.server || BY_URL}
        options={options}
        // Another server's tools are its own: they start as every one.
        onChange={(value) =>
          patch({ server: value === BY_URL ? "" : value, tools: "" }, "server")
        }
        description={
          config.server
            ? "One of the organization's MCP servers: its URL, headers and sign-in are set on the MCP servers page."
            : "Pick one of the organization's MCP servers (set up, signed in to and checked on the MCP servers page), or give one's URL here."
        }
      />
      {config.server ? (
        server?.tools ? (
          <ToolPicker
            tools={server.tools}
            value={config.tools}
            onChange={(value) => set("tools", value)}
          />
        ) : (
          <TextField
            issue="tools"
            label="Tools it may call"
            value={config.tools}
            code
            placeholder="search_articles, get_article"
            onChange={(value) => set("tools", value)}
            description="Check the server on the MCP servers page to pick from its tools. Names separated by commas; empty for every one."
          />
        )
      ) : (
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
        </>
      )}
      <CheckField
        label={CONFIRM}
        checked={config.confirm}
        onChange={(on) => set("confirm", on)}
        description={confirmAbout}
      />
      <ShowToPersonField
        checked={config.show_to_person}
        onChange={(on) => set("show_to_person", on)}
        about={showToPersonAbout}
      />
    </>
  )
}

/**
 * The tools of a server it may call, from those it listed when last
 * checked: every one (an empty list), or those ticked (comma-separated).
 */
function ToolPicker({
  tools,
  value,
  onChange,
}: {
  tools: string[]
  value: string
  onChange: (value: string) => void
}) {
  const id = React.useId()
  const { issues, key } = useFieldIssues("tools")
  const picked = value
    .split(",")
    .map((name) => name.trim())
    .filter(Boolean)
  const every = picked.length === 0
  const toggle = (name: string, on: boolean) => {
    const next = on
      ? tools.filter((tool) => tool === name || picked.includes(tool))
      : (every ? tools : picked).filter((tool) => tool !== name)
    // Every one ticked is every tool, including those it adds later.
    onChange(next.length === tools.length ? "" : next.join(", "))
  }
  return (
    <div
      role="group"
      aria-labelledby={`${id}-label`}
      data-field={key}
      className="flex flex-col gap-2"
    >
      <span id={`${id}-label`} className="text-xs font-medium text-foreground">
        Tools it may call
      </span>
      <CheckField
        label="Every tool, including any it adds later"
        checked={every}
        onChange={(on) => onChange(on ? "" : tools.join(", "))}
      />
      {!every && (
        <div
          className="flex max-h-56 flex-col gap-1.5 overflow-y-auto rounded-(--radius-item) border p-2.5"
          tabIndex={0}
        >
          {tools.map((tool) => (
            <Field key={tool} orientation="horizontal" className="items-center">
              <Checkbox
                id={`${id}-${tool}`}
                checked={picked.includes(tool)}
                onCheckedChange={(on) => toggle(tool, Boolean(on))}
              />
              <FieldLabel
                htmlFor={`${id}-${tool}`}
                className={`font-normal ${CODE}`}
              >
                {tool}
              </FieldLabel>
            </Field>
          ))}
        </div>
      )}
      {tools.length === 0 && (
        <FieldDescription>
          It listed no tools when last checked.
        </FieldDescription>
      )}
      <IssueMessages issues={issues} />
    </div>
  )
}

/** The settings of a tool of a kind. */
export function ToolFields<K extends ToolKind>({
  kind,
  bind,
  lookups,
  organizationId,
  self,
  confirmAbout,
  showToPersonAbout,
  scope,
  valuesAbout,
}: {
  kind: K
  bind: ToolBind<K>
  lookups: ToolLookups
  /** The organization it's in: whose credentials and specs it reads. */
  organizationId: string
  self?: string
  confirmAbout: string
  /** What showing a tool's calls to the people its agent talks with means; none to not offer it. */
  showToPersonAbout?: string
  /** What `{{ }}` in an HTTP tool's URL and body reads where it runs. */
  scope: Scope
  /** That in words: `{{ }}` puts in …. */
  valuesAbout: React.ReactNode
}) {
  const props = {
    ...bind,
    lookups,
    organizationId,
    self,
    confirmAbout,
    showToPersonAbout,
    scope,
    valuesAbout,
  }
  switch (kind) {
    case "saved_agent":
      return (
        <SavedAgentFields {...(props as unknown as ToolProps<"saved_agent">)} />
      )
    case "adk_workflow":
      return (
        <WorkflowFields {...(props as unknown as ToolProps<"adk_workflow">)} />
      )
    case "memory":
      return <MemoryFields {...(props as unknown as ToolProps<"memory">)} />
    case "knowledge_base":
      return (
        <KnowledgeBaseFields
          {...(props as unknown as ToolProps<"knowledge_base">)}
        />
      )
    case "http_tool":
      return (
        <HttpToolFields {...(props as unknown as ToolProps<"http_tool">)} />
      )
    case "openapi":
      return <OpenApiFields {...(props as unknown as ToolProps<"openapi">)} />
    case "mcp":
      return <McpFields {...(props as unknown as ToolProps<"mcp">)} />
    case "code_execution":
      return (
        <CodeExecutionFields
          {...(props as unknown as ToolProps<"code_execution">)}
        />
      )
    case "skills":
      return <SkillsFields {...(props as unknown as ToolProps<"skills">)} />
    case "rich_answers":
      return (
        <RichAnswersFields
          {...(props as unknown as ToolProps<"rich_answers">)}
        />
      )
  }
  return null
}
