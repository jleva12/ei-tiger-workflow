import type { StepKind } from "./model"

/*
 * Each Forge step's settings as JSON Schema (draft 2020-12), for the ADK
 * workflow format's schema (lib/agents/schema), which takes them as its
 * Forge nodes' configs.
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

/** Each kind's settings. */
export const CONFIGS: Record<StepKind, Record<string, unknown>> = {
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
