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
import { Icon } from "@/components/forge/icon"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Field, FieldDescription, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import type { ChatConfigs } from "@/features/agents/lib/model"
import { HTTP_METHODS, uid, type HeaderRow } from "@/features/steps/lib/model"
import type { KnowledgeBaseLookup, McpServerLookup } from "./chat-agent-store"

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

/** What a tool may pick from: the organization's, by ID. */
export type ToolLookups = {
  /** Agents from the Agents page. */
  agents: Record<string, string>
  workflows: Record<string, string>
  /** Where each workflow is between draft and published, for its version picker. */
  workflowVersions: Record<string, VersionChoice>
  knowledgeBases: Record<string, KnowledgeBaseLookup>
  mcpServers: Record<string, McpServerLookup>
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
  /** The agent or workflow it's in, which it can't call. */
  self?: string
  /** What a person confirming each call means where it runs. */
  confirmAbout: string
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
    </>
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
                    {found.kind === "graph"
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
          uploaded), or a graph knowledge base's code (each declaration of its
          repositories' code graphs), by meaning and by their words, and reads
          the passages that match best.
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

function HttpToolFields({ config, set, confirmAbout }: ToolProps<"http_tool">) {
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
        description={confirmAbout}
      />
    </>
  )
}

function OpenApiFields({ config, set, confirmAbout }: ToolProps<"openapi">) {
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
        description={confirmAbout}
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
  self,
  confirmAbout,
}: {
  kind: K
  bind: ToolBind<K>
  lookups: ToolLookups
  self?: string
  confirmAbout: string
}) {
  const props = { ...bind, lookups, self, confirmAbout }
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
  }
  return null
}
