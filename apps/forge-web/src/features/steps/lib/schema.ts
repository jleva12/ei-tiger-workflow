import type { StepKind } from "./model"

/*
 * Each Forge step's settings as JSON Schema (draft 2020-12), for the ADK
 * workflow format's schema (adk-workflows/lib/schema), which takes them as its
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
    query: {
      ...listOf({ id: text, name: text, value: text }),
      description:
        "Query parameters, added to the URL's own: each name and value a template, filled in then percent-encoded. A value that's one {{ }} coming to nothing leaves its parameter out; a list sends it once per item.",
    },
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
    credential: {
      ...text,
      description:
        "One of the organization's credentials, by ID: its auth (an API key, a token, OAuth) goes with each request, and only to the URLs it's for. Empty for none.",
    },
  },
  bash: {
    command: {
      ...text,
      description:
        "A bash script, run as it's written (never filled in with {{ }}): it reads the run's data from its environment variables, e.g. gh repo clone \"$REPO\". bash stops at the first command that fails (-e -o pipefail).",
    },
    cwd: {
      ...text,
      description:
        "The folder it runs in, a template: a folder of the run's folder (relative, or an absolute path inside it), made when it isn't there; empty for the run's folder itself. One outside it takes the Error way.",
    },
    env: {
      ...listOf({ id: text, name: text, value: text }),
      description:
        "Environment variables: each name (letters, digits and _) and its value, a template. WORKSPACE is always the run's folder.",
    },
    timeout_seconds: count(1),
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
  filter: {
    items: { ...text, description: "A JSONata expression giving a list." },
    item_name: {
      type: "string",
      pattern: "^[A-Za-z_][A-Za-z0-9_]*$",
      description: "What the condition calls the item it's on.",
    },
    condition: {
      ...text,
      description:
        "A JSONata condition, read per item (as item_name, with index); the step hands on {count, results}: the items it's true for, in order.",
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
        "In order: the first rule whose JSONata condition is true is taken (mode first), or every one, at once (mode all).",
    },
    mode: {
      ...oneOf("first", "all"),
      description:
        "first takes the first rule that holds; all takes every one that holds, at once.",
    },
  },
  loop: {
    items: { ...text, description: "A JSONata expression giving a list." },
    item_name: { type: "string", pattern: "^[A-Za-z_][A-Za-z0-9_]*$" },
    max_iterations: count(1),
    concurrency: count(1),
  },
  for_each: {
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
