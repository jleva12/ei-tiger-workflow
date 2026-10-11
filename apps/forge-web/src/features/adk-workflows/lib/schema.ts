import { CONFIGS as CHAT_CONFIGS } from "@/features/agents/lib/schema"
import { THINKING_LEVELS } from "@/features/steps/lib/model"
import { CONFIGS as STEP_CONFIGS } from "@/features/steps/lib/schema"
import { AGENT_FORMAT } from "./document"
import { FILE_TYPE_IDS } from "./files"
import {
  AGENT_KINDS,
  FORGE_KINDS,
  LLM_TOOL_KINDS,
  SUB_AGENT_ID,
  SUB_AGENT_KINDS,
  type AgentKind,
  type SubAgentKind,
} from "./model"
import { TOOL_ID, TOOL_KINDS } from "./tools"

/*
 * The agent format as a JSON Schema (draft 2020-12): what the admin API
 * checks every agent it saves against (agent.schema.json, written by
 * scripts/generate-agent-schema.mjs). It describes what `document.ts`
 * writes; change the two together.
 */

const text = { type: "string" } as const
const count = (minimum: number) => ({ type: "integer", minimum }) as const
const oneOf = (...values: string[]) => ({ type: "string", enum: values })
const jsonSchema = (description: string) => ({ type: "object", description })
const subAgents = (description: string) => ({
  type: "array",
  items: { $ref: "#/$defs/sub_agent" },
  description,
})

/** What an LLM agent is set up with, as a node or a sub-agent. */
const LLM_SETTINGS = {
  description: {
    ...text,
    description:
      "What it's for: what its parent and peers read when handing off to it.",
  },
  instruction: {
    ...text,
    description:
      "What it's told to do: a template, where {{ }} puts in the run's data as it runs (input, previous, steps.<id>.output, state, a loop's item). An LLM sub-agent's answer is kept in the session's state under its ID.",
  },
  model: {
    type: "object",
    description:
      "One of the models Forge offers (its model provider configuration's): the provider's key and the model's id. Both empty: the default.",
    required: ["provider", "name"],
    additionalProperties: false,
    properties: { provider: text, name: text },
  },
  thinking_level: {
    ...oneOf("", ...THINKING_LEVELS),
    description:
      "How long the model thinks before it answers: the level if the model offers it, else the nearest it does. Empty: the model's own.",
  },
  output_schema: jsonSchema(
    "A JSON Schema its answer is held to; {} leaves it free text."
  ),
  include_contents: {
    ...oneOf("default", "none"),
    description:
      "none: it sees only what it's given, not the conversation so far.",
  },
  disallow_transfer_to_parent: { type: "boolean" },
  disallow_transfer_to_peers: { type: "boolean" },
  max_output_tokens: {
    type: ["integer", "null"],
    minimum: 1,
    description: "Null: the model's limit.",
  },
  sub_agents: subAgents("The agents it may hand off to."),
  tools: {
    type: "array",
    items: { $ref: "#/$defs/tool" },
    description:
      "What it can call: the Agents builder's tools (an MCP server, a knowledge base, an HTTP endpoint, an OpenAPI spec, an agent, a workflow), each with that builder's settings.",
  },
}

const TEAM = {
  description: { ...text, description: "What it's for." },
  sub_agents: subAgents("Its sub-agents, in the order they run."),
}

const LOOP = {
  ...TEAM,
  max_iterations: {
    ...count(1),
    description: "It stops after this many passes.",
  },
}

/** Each kind of node's settings. */
const CONFIGS: Record<AgentKind, Record<string, unknown>> = {
  start: {
    input_schema: jsonSchema(
      "A JSON Schema of what a run starts with; {} takes anything."
    ),
    allow_files: {
      type: "boolean",
      description:
        "Whether a run may start with files: each is saved as an ADK artifact of the run's session, its steps read it there (ctx.load_artifact, an LLM agent's load_artifacts tool), and state.files lists them.",
    },
    file_types: {
      type: "array",
      items: oneOf(...FILE_TYPE_IDS),
      uniqueItems: true,
      description:
        "The types of files it takes, by their extension; none takes any type.",
    },
    start_over: {
      type: "boolean",
      description:
        "When a run stops mid-way (its worker went, a hiccup) and ADK can't carry it on where it stopped, whether it starts over in a new session, every step running again. Off, the run fails and can be resubmitted. A step that fails once the run carried on fails it either way.",
    },
  },
  llm: {
    ...LLM_SETTINGS,
    mode: {
      ...oneOf("single_turn", "task"),
      description:
        "single_turn: it answers once. task: it may ask back until its task is done.",
    },
    source: {
      ...oneOf("inline", "agent"),
      description:
        "inline: set up here, with these settings. agent: an agent from the Agents page, used whole (its own instruction, model and tools); the settings above other than output_schema don't apply.",
    },
    agent: {
      ...text,
      description:
        "The agent from the Agents page (ca_…), when source is agent.",
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
    message: {
      ...text,
      description:
        "What it's sent: a template, where {{ }} puts in the run's data; empty sends what the node is handed.",
    },
    inputs: {
      type: "object",
      additionalProperties: text,
      description:
        "The fields of its input schema (state_schema), each a JSONata expression over the run's data.",
    },
  },
  sequential: TEAM,
  parallel: TEAM,
  loop_agent: LOOP,
  saved: {
    agent: {
      ...text,
      description:
        "Another of the organization's agents, by ID: run here whole.",
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
    inputs: {
      type: "object",
      additionalProperties: text,
      description:
        "The fields of its start's input_schema, each a JSONata expression over the run's data. With any filled in, they're its input (an expression that finds nothing leaves its field out); with none, what comes in is.",
    },
  },
  // Forge's workflow steps: as a forge.workflow/v1 workflow's.
  ...(Object.fromEntries(
    FORGE_KINDS.map((kind) => [kind, STEP_CONFIGS[kind]])
  ) as Record<(typeof FORGE_KINDS)[number], Record<string, unknown>>),
  human_input: {
    message: {
      ...text,
      description: "What the person is asked; {{ }} puts in the run's data.",
    },
    response_schema: jsonSchema(
      "A JSON Schema of their answer; {} takes anything."
    ),
  },
}

/**
 * What each kind of node hands on: what the next node reads as `previous`,
 * and later ones as `steps.<id>.output`. A run hands these on
 * (packages/python/adk-workflows); the builder types expressions by them.
 */
const HANDS_ON: Record<AgentKind, string> = {
  start:
    "Every node reads the run's input, held to input_schema, as input (steps.<start id>.output too).",
  llm: "Hands on its answer: JSON held to output_schema when it declares one, else text.",
  sequential: "Hands on its last sub-agent's answer.",
  parallel:
    "Hands on each sub-agent's answer, by the sub-agent's ID: {<id>: answer}.",
  loop_agent: "Hands on the answer of the sub-agent that answered last.",
  saved:
    "Its inputs, when any is filled in, else what comes in, are the other workflow's input (held to its start's input_schema); it hands on that workflow's result: its End's result.",
  human_input: "Hands on the person's answer, held to response_schema.",
  approval:
    "Hands on {approved, decided_by, comment, decided_at} by its approved or rejected way.",
  http: "Hands on {status, headers, body} by its success way, body held to output_schema when it declares one. Its error way hands on nothing; steps.<id>.error is {message, status?, body?, expression?}.",
  transform:
    "Hands on what its expression makes, held to output_schema when it declares one.",
  filter:
    "Hands on {count, results}: the items its condition is true for, in order.",
  delay: "Hands on {resumed_at}.",
  if: "Hands on what came into it; steps.<id>.output records the way it took: {branch}.",
  switch:
    "Hands on what came into it; steps.<id>.output records the way it took: {branch}.",
  match:
    "Hands on what came into it; steps.<id>.output records the way it took: {branch}, or {branches} in mode all.",
  loop: "Its body (what its each way reaches before coming back to it) runs per item, reading the item as item_name and its place as index; by its done way it hands on {count, results}, results what each item's body handed back.",
  for_each:
    "Its body (what its each way reaches; nothing leads back) runs per item, reading the item as item_name and its place as index; by its done way it hands on {count}.",
  merge:
    "Hands on what each way into it handed on, by the step it came from: {<step id>: ...}.",
  end: "Finishes its way with its result: its expression's, or what came in when it has none.",
}

/** Each kind of sub-agent's settings. */
const SUB_CONFIGS: Record<SubAgentKind, Record<string, unknown>> = {
  llm: LLM_SETTINGS,
  sequential: TEAM,
  parallel: TEAM,
  loop_agent: LOOP,
}

const kinds = Object.keys(AGENT_KINDS) as AgentKind[]

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

export const AGENT_JSON_SCHEMA = {
  $schema: "https://json-schema.org/draft/2020-12/schema",
  title: "Forge agent",
  description:
    "An organization's agent: a Google ADK graph workflow of nodes (ADK agents and other agents, and Forge's workflow steps: approvals, HTTP requests, transforms, filters, delays, If / Switch / Match, loops, for-eachs, merges, ends) joined by edges, run from its start. Every expression (JSONata; in text, inside {{ }}) reads input (the run's), previous (what the node before handed on), steps.<id>.output and .error (the nodes that ran before it), state (the session's) and, in a loop's or for-each's body, its item and index. What each kind hands on is in its config's description.",
  type: "object",
  required: ["format", "id", "name", "nodes", "edges"],
  properties: {
    format: { const: AGENT_FORMAT },
    id: text,
    name: text,
    description: text,
    organization_id: text,
    version: {
      type: "integer",
      minimum: 1,
      description:
        "Which published version this is; a draft has none (an export of one carries it).",
    },
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
        kind: { enum: kinds },
        name: {
          ...text,
          description:
            "Its ADK name is this as a Python identifier (Billing specialist: billing_specialist).",
        },
        config: { type: "object" },
        outputs: {
          type: "array",
          description:
            "The IDs of the ways out of the node, in order: next, or a branching step's (success / error, true / false, a case's or rule's ID and default / otherwise, each / done).",
          items: text,
        },
      },
      allOf: kinds.map((kind) => ({
        if: { properties: { kind: { const: kind } } },
        then: { properties: { config: { $ref: `#/$defs/config_${kind}` } } },
      })),
    },
    sub_agent: {
      type: "object",
      required: ["id", "kind", "name", "config"],
      additionalProperties: false,
      properties: {
        id: {
          type: "string",
          pattern: SUB_AGENT_ID.source,
          description:
            "Made by the builder, unique in the agent: an LLM sub-agent's answer is kept in the session's state under it.",
        },
        kind: { enum: SUB_AGENT_KINDS },
        name: text,
        config: { type: "object" },
      },
      allOf: SUB_AGENT_KINDS.map((kind) => ({
        if: { properties: { kind: { const: kind } } },
        then: {
          properties: { config: { $ref: `#/$defs/sub_config_${kind}` } },
        },
      })),
    },
    tool: {
      type: "object",
      required: ["id", "kind", "name", "config"],
      additionalProperties: false,
      properties: {
        id: {
          type: "string",
          pattern: TOOL_ID.source,
          description: "Made by the builder, unique among its agent's tools.",
        },
        kind: { enum: LLM_TOOL_KINDS },
        name: {
          ...text,
          description:
            "What it's called: an HTTP tool's or knowledge base's name for the model, as a Python identifier.",
        },
        config: { type: "object" },
      },
      allOf: LLM_TOOL_KINDS.map((kind) => ({
        if: { properties: { kind: { const: kind } } },
        then: {
          properties: { config: { $ref: `#/$defs/tool_config_${kind}` } },
        },
      })),
    },
    edge: {
      type: "object",
      required: ["id", "source", "source_output", "target"],
      additionalProperties: false,
      properties: {
        id: text,
        source: { $ref: "#/$defs/nodeId" },
        source_output: {
          ...text,
          description: "One of the source node's outputs.",
        },
        target: { $ref: "#/$defs/nodeId" },
      },
    },
    ...Object.fromEntries(
      kinds.map((kind) => [
        `config_${kind}`,
        settings(
          CONFIGS[kind],
          `${AGENT_KINDS[kind].summary} ${HANDS_ON[kind]}`
        ),
      ])
    ),
    ...Object.fromEntries(
      LLM_TOOL_KINDS.map((kind) => [
        `tool_config_${kind}`,
        settings(CHAT_CONFIGS[kind], `A tool: ${TOOL_KINDS[kind].summary}`),
      ])
    ),
    ...Object.fromEntries(
      SUB_AGENT_KINDS.map((kind) => [
        `sub_config_${kind}`,
        settings(
          SUB_CONFIGS[kind],
          `A sub-agent: ${AGENT_KINDS[kind].summary}`
        ),
      ])
    ),
  },
} as const
