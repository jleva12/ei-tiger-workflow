import { WORKFLOW_FORMAT } from "./document"
import { STEP_KINDS, THINKING_LEVELS, type StepKind } from "./model"

/*
 * The workflow format as a JSON Schema (draft 2020-12): what a runner, or
 * the API when there is one, can check a workflow's JSON against. It
 * describes what `document.ts` writes; change the two together.
 */

const text = { type: "string" } as const
const count = (minimum: number) => ({ type: "integer", minimum }) as const
const oneOf = (...values: string[]) => ({ type: "string", enum: values })

const listOf = (properties: Record<string, unknown>) => ({
  type: "array",
  items: {
    type: "object",
    required: Object.keys(properties),
    additionalProperties: false,
    properties,
  },
})

/** Each kind's settings (the agent format takes its Forge steps' from here). */
export const CONFIGS: Record<StepKind, Record<string, unknown>> = {
  entry: {
    trigger: oneOf("manual", "event", "schedule"),
    event_types: {
      type: "array",
      items: text,
      uniqueItems: true,
      description:
        "With an event trigger: the keys of the event types that start it.",
    },
    cron: {
      ...text,
      description: "With a schedule trigger: a five-field cron expression.",
    },
    timezone: {
      ...text,
      description: "An IANA time zone, e.g. Europe/London.",
    },
    input_schema: {
      type: "object",
      description:
        "With a manual trigger: a JSON Schema of the input whoever starts it gives; {} takes anything.",
    },
  },
  agent: {
    instructions: text,
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
    output: oneOf("text", "json"),
    output_schema: {
      ...text,
      description: "With json output: a JSON Schema, as text.",
    },
  },
  approval: {
    message: text,
    approvers: oneOf("org:admin", "org:member"),
    timeout_hours: {
      ...count(0),
      description: "0 waits until someone decides.",
    },
  },
  http: {
    method: oneOf("GET", "POST", "PUT", "PATCH", "DELETE"),
    url: text,
    headers: listOf({ id: text, name: text, value: text }),
    body: {
      ...text,
      description:
        "A JSONata expression making the body; an object or list is sent as JSON.",
    },
    timeout_seconds: count(1),
    retries: count(0),
    output_schema: {
      type: "object",
      description:
        "A JSON Schema of the response's body; {} leaves it undeclared.",
    },
  },
  transform: {
    expression: {
      ...text,
      description: "A JSONata expression: what the step hands on.",
    },
    output_schema: {
      type: "object",
      description: "A JSON Schema of what it makes; {} leaves it undeclared.",
    },
  },
  delay: {
    amount: { type: "number", exclusiveMinimum: 0 },
    unit: oneOf("seconds", "minutes", "hours", "days"),
  },
  subworkflow: {
    workflow: { ...text, description: "Another workflow's ID." },
    wait: { type: "boolean" },
  },
  if: {
    condition: {
      ...text,
      description: "A JSONata expression; true takes the True way.",
    },
  },
  switch: {
    value: {
      ...text,
      description:
        "A JSONata expression; the case it equals (as text) is taken.",
    },
    cases: listOf({ id: text, value: text }),
  },
  match: {
    arms: {
      ...listOf({ id: text, label: text, condition: text }),
      description:
        "In order: the first rule whose JSONata condition is true is taken.",
    },
  },
  loop: {
    items: { ...text, description: "A JSONata expression giving a list." },
    item_name: { type: "string", pattern: "^[A-Za-z_][A-Za-z0-9_]*$" },
    max_iterations: count(1),
    concurrency: count(1),
  },
  merge: { mode: oneOf("all", "any") },
  end: {
    outcome: oneOf("succeeded", "failed"),
    result: {
      ...text,
      description: "A JSONata expression giving the workflow's result.",
    },
  },
}

const kinds = Object.keys(STEP_KINDS) as StepKind[]

export const WORKFLOW_JSON_SCHEMA = {
  $schema: "https://json-schema.org/draft/2020-12/schema",
  title: "Forge workflow",
  description:
    "An organization's workflow: steps (nodes) joined by connections (edges), run from its start step.",
  type: "object",
  required: ["format", "id", "name", "entry", "nodes", "edges"],
  properties: {
    format: { const: WORKFLOW_FORMAT },
    id: text,
    name: text,
    description: text,
    organization_id: text,
    entry: {
      type: ["string", "null"],
      description: "The ID of the start step every run starts at.",
    },
    nodes: { type: "array", items: { $ref: "#/$defs/node" } },
    edges: { type: "array", items: { $ref: "#/$defs/edge" } },
    layout: {
      type: "object",
      description:
        "Where the builder draws each step, by ID. Runners ignore it.",
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
    stepId: {
      type: "string",
      pattern: "^[a-z][a-z0-9_]{0,47}$",
      description:
        "Made by the builder, unique in the workflow and never reused; expressions read a step's output as steps.<id>.output.",
    },
    node: {
      type: "object",
      required: ["id", "kind", "name", "config", "outputs"],
      properties: {
        id: { $ref: "#/$defs/stepId" },
        kind: { enum: kinds },
        name: text,
        config: { type: "object" },
        outputs: {
          type: "array",
          description: "The IDs of the ways out of the step, in order.",
          items: text,
        },
      },
      allOf: kinds.map((kind) => ({
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
        source: { $ref: "#/$defs/stepId" },
        source_output: {
          ...text,
          description: "One of the source step's outputs.",
        },
        target: { $ref: "#/$defs/stepId" },
      },
    },
    ...Object.fromEntries(
      kinds.map((kind) => [
        `config_${kind}`,
        {
          type: "object",
          description: STEP_KINDS[kind].summary,
          required: Object.keys(CONFIGS[kind]),
          additionalProperties: false,
          properties: CONFIGS[kind],
        },
      ])
    ),
  },
} as const
