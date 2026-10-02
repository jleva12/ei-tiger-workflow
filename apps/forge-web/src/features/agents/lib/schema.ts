import { HTTP_METHODS, THINKING_LEVELS } from "@/features/steps/lib/model"
import { CHAT_AGENT_FORMAT } from "./document"
import {
  CHAT_KIND_LIST,
  CHAT_KINDS,
  HANDS_OFF,
  TOOLS,
  type ChatKind,
} from "./model"

/*
 * The chat agent format as a JSON Schema (draft 2020-12): what
 * `document.ts` writes, for the JSON tab now and for the admin API to
 * check agents against once it keeps them. Change the two together.
 */

const text = { type: "string" } as const
const bool = { type: "boolean" } as const
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
}

const CONFIGS: Record<ChatKind, Record<string, unknown>> = {
  agent: LLM,
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
  },
  saved_agent: {
    agent: {
      ...text,
      description: "Another of the organization's agents, by ID.",
    },
  },
  adk_workflow: {
    workflow: {
      ...text,
      description:
        "One of the organization's ADK workflows, by ID, called as a tool.",
    },
  },
  memory: {
    mode: {
      ...oneOf("on_demand", "every_turn"),
      description:
        "on_demand: it looks memories up when it decides to (load_memory); every_turn: they come with each message (preload_memory).",
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
      description: "Where it calls; {name} puts in an argument of that name.",
    },
    headers,
    parameters: jsonSchema(
      "A JSON Schema of the arguments the model fills in; {} takes none."
    ),
    confirm: {
      ...bool,
      description: "A person confirms each call before it's made.",
    },
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
        "The operation IDs it may call, comma-separated; empty for all.",
    },
    confirm: bool,
  },
  mcp: {
    transport: oneOf("streamable_http", "sse"),
    url: text,
    headers,
    tools: {
      ...text,
      description:
        "The server's tools it may call, comma-separated; empty for all.",
    },
    confirm: bool,
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

export const CHAT_AGENT_JSON_SCHEMA = {
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
        kind: { enum: CHAT_KIND_LIST },
        name: {
          ...text,
          description:
            "An agent's ADK name, or an HTTP tool's name, is this as a Python identifier.",
        },
        config: { type: "object" },
        outputs: {
          type: "array",
          description: `An agent's ways out: ${TOOLS} (what it can call) and ${HANDS_OFF} (who it hands off to); none for the rest.`,
          items: text,
        },
      },
      allOf: CHAT_KIND_LIST.map((kind) => ({
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
        source_output: { enum: [TOOLS, HANDS_OFF] },
        target: { $ref: "#/$defs/nodeId" },
      },
    },
    ...Object.fromEntries(
      CHAT_KIND_LIST.map((kind) => [
        `config_${kind}`,
        settings(CONFIGS[kind], CHAT_KINDS[kind].summary),
      ])
    ),
  },
} as const
