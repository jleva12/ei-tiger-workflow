import type { Root, Scope } from "@/features/steps/lib/scope"
import { fromJsonSchema, t, type DataType } from "@/features/steps/lib/types"

/*
 * What a chat UI sends to run the chat agent: ADK's run request
 * (`POST …/run_sse`). Its fields are fixed; so are the two state keys the
 * model picker sends. The agent declares the rest of the state it's sent
 * (its `state_schema`), and its instructions read all of it in templates:
 * `{{ request.userId }}`, `{{ state.customer_tier }}`.
 */

export type InputField = {
  name: string
  type: DataType
  /** One line: what it is. */
  detail: string
}

const part = t.object(
  {
    text: t.string("Text the person wrote."),
    inlineData: t.object(
      {
        mimeType: t.string("The file's media type."),
        data: t.string("The file, base64-encoded."),
      },
      "A file they attached."
    ),
    functionResponse: t.object(
      {
        id: t.string("The call it answers."),
        name: t.string("The tool that was called."),
        response: t.unknown("The answer."),
      },
      "An answer to one of the agent's tool calls, such as a confirmation."
    ),
  },
  "One part of the message.",
  { open: true }
)

/** The run request's own fields, as every chat UI sends them. */
export const REQUEST_FIELDS: InputField[] = [
  {
    name: "appName",
    type: t.string("The agent app the conversation belongs to."),
    detail: "The agent app",
  },
  {
    name: "userId",
    type: t.string("Who's chatting: the signed-in person's ID."),
    detail: "Who's chatting",
  },
  {
    name: "sessionId",
    type: t.string("The conversation, created before its first message."),
    detail: "The conversation",
  },
  {
    name: "newMessage",
    type: t.object(
      {
        role: t.string("Always user.", { enum: ["user"] }),
        parts: t.array(part, "The message's text, files and tool answers."),
      },
      "The person's turn: a Gemini Content.",
      { required: ["role", "parts"] }
    ),
    detail: "Their message",
  },
  {
    name: "streaming",
    type: t.boolean("Text arrives as it's written."),
    detail: "Stream the reply",
  },
]

/** The state keys the model picker sends with every message. */
export const FIXED_STATE_FIELDS: InputField[] = [
  {
    name: "model",
    type: t.string(
      "The model the person picked, provider/model; empty for the default."
    ),
    detail: "The model they picked",
  },
  {
    name: "thinking_level",
    type: t.string("How long the model thinks.", {
      enum: ["off", "minimal", "low", "medium", "high", "xhigh"],
    }),
    detail: "How long it thinks",
  },
]

/** State keys the agent can't declare: the picker's, and ADK's scoped prefixes. */
export const RESERVED_STATE = new Set(FIXED_STATE_FIELDS.map((f) => f.name))
export const RESERVED_PREFIXES = ["app:", "user:", "temp:"]

/** The fields an agent's state schema declares, with their types. */
export function declaredState(schema: Record<string, unknown>): {
  fields: [string, DataType][]
  required: Set<string>
} {
  const type =
    Object.keys(schema).length > 0 ? fromJsonSchema(schema) : undefined
  if (type?.kind !== "object") return { fields: [], required: new Set() }
  return {
    // A field still being named isn't one yet.
    fields: Object.entries(type.properties).filter(([name]) => name.trim()),
    required: new Set(type.required ?? []),
  }
}

/** What's wrong with a declared state key's name, if anything. */
export function stateNameProblem(name: string): string | null {
  if (RESERVED_STATE.has(name))
    return `${name} is sent by the model picker already; name it something else.`
  const prefix = RESERVED_PREFIXES.find((p) => name.startsWith(p))
  if (prefix)
    return `${prefix} state is ADK's own and can't be sent from the chat; drop the prefix.`
  if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(name))
    return `${name} needs letters, digits and underscores, starting with a letter, to be read as state.${name}.`
  return null
}

/** The state an agent is sent: the picker's keys and those it declares. */
export function stateType(schema: Record<string, unknown>): DataType {
  const { fields, required } = declaredState(schema)
  const properties: Record<string, DataType> = Object.fromEntries(
    FIXED_STATE_FIELDS.map((f) => [f.name, f.type])
  )
  for (const [name, type] of fields) {
    if (!RESERVED_STATE.has(name)) properties[name] = type
  }
  return t.object(
    properties,
    "The session's state: what the chat sends in stateDelta, kept for the conversation.",
    // ADK's own keys and what tools write are there too, unchecked.
    { open: true, required: [...required].filter((n) => n in properties) }
  )
}

/** What an agent's instructions can read, completed and checked as they're typed. */
export function chatScope(schema: Record<string, unknown>): Scope {
  const roots: Root[] = [
    {
      name: "request",
      detail: "What the chat sent to run this turn",
      type: t.object(
        Object.fromEntries(REQUEST_FIELDS.map((f) => [f.name, f.type])),
        "The run request: who's chatting, in which conversation, and their message.",
        { required: REQUEST_FIELDS.map((f) => f.name) }
      ),
    },
    {
      name: "state",
      detail: "The conversation's state",
      type: stateType(schema),
    },
  ]
  return {
    roots: new Map(roots.map((root) => [root.name, root])),
    steps: new Map(),
    allSteps: new Map(),
  }
}
