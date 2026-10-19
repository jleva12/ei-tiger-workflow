import { RICH_ANSWER_COMPONENTS } from "@/components/forge/generative-ui/catalog"

/** A team UI component's key: `org.` and lowercase words joined by hyphens (forge_agent_runtime.generative_ui). */
const TEAM_COMPONENT_KEY = "^org\\.[a-z0-9]+(?:-[a-z0-9]+)*$"
import { HTTP_METHODS, THINKING_LEVELS } from "@/features/steps/lib/model"
import { chatKindInfo, KNOWN_CHAT_KINDS } from "./catalog"
import { CHAT_AGENT_FORMAT } from "./document"
import { MAX_RULE_DEPTH } from "./access"
import { INTAKE_FIELD_TYPES } from "./intake"
import { HANDS_OFF, ON_COMPLETE, TOOLS, type ChatKind } from "./model"

/*
 * The chat agent format as a JSON Schema (draft 2020-12): what
 * `document.ts` writes, for the JSON tab now and for the admin API to
 * check agents against once it keeps them. Change the two together. Each
 * kind's settings are described as the node catalog has the kind, so the
 * schema is made once it's installed.
 */

const text = { type: "string" } as const
const bool = { type: "boolean" } as const
/** A tool's calls shown in full to the people its agent talks with (forge_agent_runtime.views). */
const showToPerson = {
  ...bool,
  description:
    "The people the agent talks with see what it was called with and what it answered; otherwise only that it ran. Whoever builds the agent always sees both.",
} as const
const oneOf = (...values: readonly string[]) => ({
  type: "string",
  enum: [...values],
})
const jsonSchema = (description: string) => ({ type: "object", description })
const headers = {
  type: "array",
  items: {
    type: "object",
    required: ["id", "name", "value"],
    additionalProperties: false,
    properties: { id: text, name: text, value: text },
  },
}

const LLM = {
  description: {
    ...text,
    description:
      "What it's for: what agents read when they hand off to it, or call it.",
  },
  instruction: { ...text, description: "Its system instruction." },
  model: {
    type: "object",
    description:
      "One of the models Forge offers: its provider's key and its id. Both empty: the default.",
    required: ["provider", "name"],
    additionalProperties: false,
    properties: { provider: text, name: text },
  },
  thinking_level: {
    ...oneOf("", ...THINKING_LEVELS),
    description:
      "How long the model thinks: the nearest level it offers. Empty: the model's own.",
  },
  max_output_tokens: {
    type: ["integer", "null"],
    minimum: 1,
    description: "Null: the model's limit.",
  },
  ask_person: {
    ...bool,
    description:
      "It may stop mid-turn to ask the person something (ADK's adk_request_input); the chat shows a form for the answer. Not in workflows' runs.",
  },
}

/** One permission, or a group of rules: `depth` more groups may nest in it. */
const accessRule = (depth: number): Record<string, unknown> => {
  const permission = {
    type: "object",
    required: ["permission"],
    additionalProperties: false,
    properties: {
      permission: {
        type: "integer",
        minimum: 1,
        description: "One of the organization's agent permissions, by ID.",
      },
    },
  }
  if (depth === 0) return permission
  const items = { type: "array", minItems: 1, items: accessRule(depth - 1) }
  const group = (key: "all" | "any") => ({
    type: "object",
    required: [key],
    additionalProperties: false,
    properties: { [key]: items },
  })
  return { oneOf: [permission, group("all"), group("any")] }
}

/** Who may use an agent: its node's rule (`features/agents/lib/access.ts`). */
const ACCESS = {
  access: {
    oneOf: [{ type: "null" }, accessRule(MAX_RULE_DEPTH)],
    description:
      'Which of the organization\'s agent permissions someone needs to use it: {"permission": <id>}, {"all": [rules]} (every one) or {"any": [rules]} (at least one), groups nesting 3 deep. Null: anyone who may call the agent.',
  },
}

/** Each kind's settings: a workflow's LLM agents' tools take them too. */
export const CONFIGS: Record<ChatKind, Record<string, unknown>> = {
  agent: {
    ...LLM,
    state_schema: jsonSchema(
      "A JSON Schema of the state the chat sends in stateDelta besides model and thinking_level; {} declares none."
    ),
    ...ACCESS,
  },
  sub_agent: {
    ...LLM,
    mode: {
      ...oneOf("chat", "task", "single_turn"),
      description:
        "Handed off to: chat takes over the conversation; task does one task and hands back; single_turn answers once.",
    },
    disallow_transfer_to_parent: bool,
    disallow_transfer_to_peers: bool,
    include_contents: {
      ...oneOf("default", "none"),
      description: "none: it sees only what it's given.",
    },
    ...ACCESS,
  },
  intake_agent: {
    ...LLM,
    fields: {
      type: "array",
      description:
        "The record it collects, in the order it asks: each field's key, type, whether it's required and when it applies.",
      items: {
        type: "object",
        required: ["key"],
        additionalProperties: false,
        properties: {
          id: {
            ...text,
            description: "The builder's, for telling fields apart.",
          },
          key: {
            type: "string",
            pattern: "^[a-z][a-z0-9_]{0,63}$",
            description: "What the record and the model call it.",
          },
          label: text,
          type: oneOf(...INTAKE_FIELD_TYPES),
          options: {
            type: "array",
            items: text,
            description: "A choice's options.",
          },
          description: {
            ...text,
            description: "What it is, and how to ask for it.",
          },
          why: {
            ...text,
            description: "Why it's asked, for when the person asks.",
          },
          required: bool,
          when: {
            ...text,
            description:
              "A JSONata condition on the record's values: it applies only when this holds. Empty: always.",
          },
          infer: {
            ...bool,
            description:
              "The model may work it out from what the person said; the person confirms it.",
          },
          confirm: {
            ...bool,
            description:
              "The person confirms its value before the intake finishes.",
          },
          prefill: {
            ...bool,
            description: "The agent handing off may fill it in.",
          },
          section: {
            ...text,
            description: "Its section's ID; empty for none.",
          },
        },
      },
    },
    sections: {
      type: "array",
      description: "Fields asked together, each unlocked on a condition.",
      items: {
        type: "object",
        required: ["id"],
        additionalProperties: false,
        properties: {
          id: text,
          title: text,
          when: {
            ...text,
            description:
              "A JSONata condition on the record's values: its fields apply only when this holds. Empty: always.",
          },
        },
      },
    },
    confirm_summary: {
      ...bool,
      description:
        "It reads back a summary and waits for the person's yes before it finishes.",
    },
    allow_cancel: {
      ...bool,
      description: "The person may stop it: it hands back as cancelled.",
    },
    allow_escalate: {
      ...bool,
      description: "It may hand back as escalated, for a person.",
    },
    ...ACCESS,
  },
  saved_agent: {
    agent: {
      ...text,
      description: "Another of the organization's agents, by ID.",
    },
    ...ACCESS,
  },
  adk_workflow: {
    workflow: {
      ...text,
      description:
        "One of the organization's workflows, by ID, called as a tool.",
    },
    version: {
      oneOf: [
        { type: "integer", minimum: 1 },
        { const: "draft" },
        { type: "null" },
      ],
      description:
        "Which of its versions runs: a published one, its draft, or null for its latest published (as each run starts).",
    },
    wait_seconds: {
      type: "integer",
      minimum: 1,
      description:
        "Seconds the agent waits for the run before it's told the run is still going, with its ID; at most the deployment's cap (FORGE_AGENT_WORKFLOW_WAIT_MAX). Missing: the deployment's default.",
    },
  },
  memory: {
    mode: {
      ...oneOf("on_demand", "every_turn"),
      description:
        "on_demand: it looks memories up when it decides to (load_memory); every_turn: they come with each message (preload_memory).",
    },
  },
  knowledge_base: {
    knowledge_bases: {
      type: "array",
      items: text,
      description:
        "The organization's knowledge bases it searches, by ID: their documents, parsed, chunked and embedded.",
    },
    description: {
      ...text,
      description:
        "What the model is told it finds there; empty to describe the knowledge bases.",
    },
    max_results: {
      type: "integer",
      minimum: 1,
      maximum: 20,
      description: "The most passages one search answers.",
    },
  },
  http_tool: {
    description: {
      ...text,
      description: "What it does: the model decides when to call it by this.",
    },
    method: oneOf(...HTTP_METHODS),
    url: {
      ...text,
      description:
        "Where it calls: {name} puts in the model's argument of that name, {{ expression }} one of the run's values. A query parameter that's one argument is left out when the model doesn't send it.",
    },
    query: {
      ...headers,
      description:
        "Query parameters, added to the URL's own: each name and value a template like the URL, filled in then percent-encoded. A value that's one placeholder coming to nothing (an argument the model didn't send) leaves its parameter out; a list sends it once per item.",
    },
    headers,
    body: {
      ...text,
      description:
        "Its JSON body (POST, PUT, PATCH), with placeholders as its URL has; one that's a whole value goes in as it is. Empty for none.",
    },
    parameters: jsonSchema(
      "A JSON Schema of the arguments: their types and descriptions. The model is offered those the URL and body name."
    ),
    credential: {
      ...text,
      description:
        "One of the organization's credentials, by ID: its auth (an API key, a token, OAuth) goes with each call, and only to the URLs it's for. Empty for none.",
    },
    confirm: {
      ...bool,
      description: "A person confirms each call before it's made.",
    },
    show_to_person: showToPerson,
  },
  openapi: {
    source: oneOf("url", "inline"),
    url: { ...text, description: "Where the spec is, when its source is url." },
    spec: {
      ...text,
      description:
        "An OpenAPI 3 spec, JSON or YAML, when its source is inline.",
    },
    operations: {
      ...text,
      description:
        "The operations it may call, comma-separated: their tool names (the operationId in snake case) or operation IDs; empty for all.",
    },
    credential: {
      ...text,
      description:
        "One of the organization's credentials, by ID: its auth goes with each call, and only to the URLs it's for. Empty for none.",
    },
    confirm: bool,
    show_to_person: showToPerson,
  },
  mcp: {
    server: {
      ...text,
      description:
        "One of the organization's MCP servers, by ID: its URL, headers and sign-in come from there. Empty for one set up by transport, url and headers.",
    },
    transport: oneOf("streamable_http", "sse"),
    url: text,
    headers,
    tools: {
      ...text,
      description:
        "The server's tools it may call, comma-separated; empty for all.",
    },
    confirm: bool,
    show_to_person: showToPerson,
  },
  skills: {
    skills: {
      type: "array",
      items: text,
      uniqueItems: true,
      description:
        "The organization's skills it has, by ID (its skills vault): the model is told each one's name and description and loads one when a task calls for it.",
    },
    run_scripts: {
      ...bool,
      description:
        "The model may run the skills' scripts, where the runtime runs code; otherwise it reads a script and follows it itself.",
    },
  },
  rich_answers: {
    components: {
      type: "array",
      items: {
        anyOf: [
          { enum: [...RICH_ANSWER_COMPONENTS] },
          {
            type: "string",
            pattern: TEAM_COMPONENT_KEY,
            maxLength: 64,
            description:
              "One of the organization's team UI components, by key: found where it runs (forge_agent_runtime.generative_ui).",
          },
        ],
      },
      uniqueItems: true,
      description:
        "The Generative UI components the agent may draw with present, base ones by name and the organization's team ones by key (org.…); only these go into its model calls.",
    },
  },
  code_execution: {
    confirm: {
      ...bool,
      description:
        "A person confirms each run, seeing the code, before it runs.",
    },
    show_to_person: showToPerson,
  },
}

const settings = (
  properties: Record<string, unknown>,
  description: string
) => ({
  type: "object",
  description,
  required: Object.keys(properties),
  additionalProperties: false,
  properties,
})

const FORMAT = {
  $schema: "https://json-schema.org/draft/2020-12/schema",
  title: "Forge chat agent",
  description:
    "An organization's chat agent: one Google ADK LlmAgent people talk to, the tools it can call and the agents it hands off to, joined by edges from an agent's ways out.",
  type: "object",
  required: ["format", "id", "name", "nodes", "edges"],
  properties: {
    format: { const: CHAT_AGENT_FORMAT },
    id: text,
    name: text,
    description: text,
    organization_id: text,
    nodes: { type: "array", items: { $ref: "#/$defs/node" } },
    edges: { type: "array", items: { $ref: "#/$defs/edge" } },
    layout: {
      type: "object",
      description:
        "Where the builder draws each node, by ID. Runners ignore it.",
      additionalProperties: {
        type: "object",
        required: ["x", "y"],
        properties: { x: { type: "number" }, y: { type: "number" } },
      },
    },
    created_at: { type: "string", format: "date-time" },
    updated_at: { type: "string", format: "date-time" },
    version: {
      description:
        "Which version this is: a published version's number, or draft. The builder leaves it out; exports carry it.",
      oneOf: [{ type: "integer", minimum: 1 }, { const: "draft" }],
    },
    dependencies: {
      type: "object",
      description:
        'The saved agents it uses, each a chat agent document of its own, by "<id>@<version>": what an exported agent needs to run on its own.',
      additionalProperties: { type: "object" },
    },
    skills: {
      type: "object",
      description:
        "The skills its Skills tools (and its saved agents') use, by ID, each with its name, description, instructions and files: what an exported agent needs to run on its own.",
      additionalProperties: { type: "object" },
    },
    ui_components: {
      type: "object",
      description:
        "The team UI components its Rich answers nodes (and its saved agents') pick, by key, each a published forge.ui_component/v1 definition: what an exported agent needs to run on its own. The hosted runtime uses the organization's instead.",
      propertyNames: { pattern: TEAM_COMPONENT_KEY },
      additionalProperties: { type: "object" },
    },
  },
  $defs: {
    nodeId: {
      type: "string",
      pattern: "^[a-z][a-z0-9_]{0,47}$",
      description: "Made by the builder, unique in the agent and never reused.",
    },
    node: {
      type: "object",
      required: ["id", "kind", "name", "config", "outputs"],
      properties: {
        id: { $ref: "#/$defs/nodeId" },
        kind: { enum: KNOWN_CHAT_KINDS },
        name: {
          ...text,
          description:
            "An agent's ADK name, or an HTTP tool's name, is this as a Python identifier.",
        },
        config: { type: "object" },
        outputs: {
          type: "array",
          description: `An agent's ways out: ${TOOLS} (what it can call) and ${HANDS_OFF} (who it hands off to); an intake agent's ${TOOLS} and ${ON_COMPLETE} (the workflows it starts when it completes); none for the rest.`,
          items: text,
        },
      },
      allOf: KNOWN_CHAT_KINDS.map((kind) => ({
        if: { properties: { kind: { const: kind } } },
        then: { properties: { config: { $ref: `#/$defs/config_${kind}` } } },
      })),
    },
    edge: {
      type: "object",
      required: ["id", "source", "source_output", "target"],
      additionalProperties: false,
      properties: {
        id: text,
        source: { $ref: "#/$defs/nodeId" },
        source_output: { enum: [TOOLS, HANDS_OFF, ON_COMPLETE] },
        target: { $ref: "#/$defs/nodeId" },
      },
    },
  },
} as const

/** The format's JSON Schema, each kind described as the node catalog has it. */
export const chatAgentJsonSchema = () => ({
  ...FORMAT,
  $defs: {
    ...FORMAT.$defs,
    ...Object.fromEntries(
      KNOWN_CHAT_KINDS.map((kind) => [
        `config_${kind}`,
        settings(CONFIGS[kind], chatKindInfo(kind).summary),
      ])
    ),
  },
})
