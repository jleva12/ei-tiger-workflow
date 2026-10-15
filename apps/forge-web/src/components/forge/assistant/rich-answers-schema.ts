import {
  buildPresentParameters,
  defaultGenerativeUILibrary,
  type GenerativeUILibrary,
} from "@assistant-ui/generative-ui/react"
import { z } from "zod"

/*
 * What an agent with Rich answers may draw with its `present` call:
 * assistant-ui's whole Generative UI vocabulary (its 35 components, with
 * their own props and descriptions, so a tree renders, or converts to Slack
 * and Teams, as the package's would) and Choices, Forge's own quick replies.
 * Each Rich answers node picks the components its agent gets; only those go
 * into the agent's model calls.
 *
 * `presentCatalog()` is what the agent runtime builds an agent's `present`
 * schema from: each component's own props, its group, what it needs and
 * whether it acts. `npm run generate:agent-schema` writes it to the runtime
 * package (`present.catalog.json`). `rich-answers.tsx` draws the calls.
 */

/** Forge's own: buttons whose pick is the person's next message. */
const Choices = {
  description:
    "Buttons the person clicks instead of typing an answer: a click sends that option's reply to you as their next message, and the buttons turn off. Offer the next steps they're likely to want, or a pick between a few options.",
  properties: z.object({
    options: z
      .array(
        z.object({
          label: z.string().describe("What the button says, in a few words."),
          reply: z
            .string()
            .optional()
            .describe(
              "What the person says by clicking it, as their message; its label by default."
            ),
        })
      )
      .describe("The options, two to five."),
  }),
}

/** The components, by group, in the order the model is offered them. */
export const RICH_ANSWER_GROUPS = [
  {
    id: "layout",
    label: "Layout",
    components: [
      "Card",
      "Row",
      "Col",
      "Tabs",
      "Carousel",
      "Box",
      "Spacer",
      "Divider",
    ],
  },
  {
    id: "text",
    label: "Text",
    components: [
      "Header",
      "Text",
      "Caption",
      "Markdown",
      "Badge",
      "Icon",
      "Alert",
      "Citation",
    ],
  },
  {
    id: "data",
    label: "Data",
    components: [
      "Fact",
      "Table",
      "Chart",
      "Steps",
      "Timeline",
      "ListView",
      "ListViewItem",
    ],
  },
  { id: "media", label: "Media", components: ["Image", "Diagram", "Map"] },
  {
    id: "input",
    label: "Input",
    components: [
      "Choices",
      "Button",
      "Select",
      "Input",
      "DatePicker",
      "Checkbox",
      "Slider",
      "RadioGroup",
      "CheckboxGroup",
      "Form",
    ],
  },
] as const

export type RichAnswerComponent =
  (typeof RICH_ANSWER_GROUPS)[number]["components"][number]

/** Every component, in the offered order. */
export const RICH_ANSWER_COMPONENTS: readonly RichAnswerComponent[] =
  RICH_ANSWER_GROUPS.flatMap((group) => group.components)

/** What a new Rich answers node (or one older than the picks) offers. */
export const DEFAULT_RICH_ANSWER_COMPONENTS: readonly RichAnswerComponent[] = [
  "Card",
  "Row",
  "Col",
  "Fact",
  "Table",
  "Chart",
  "Steps",
  "Choices",
]

/** Components that are no use without others: picking one brings these. */
export const RICH_ANSWER_REQUIRES: Partial<
  Record<RichAnswerComponent, readonly RichAnswerComponent[]>
> = {
  Carousel: ["Card"],
  ListView: ["ListViewItem"],
  ListViewItem: ["ListView"],
}

/** Components that act through an `_action`, so the model is told how. */
export const RICH_ANSWER_ACTING: readonly RichAnswerComponent[] = [
  "Button",
  "Select",
  "Input",
  "DatePicker",
  "Checkbox",
  "Slider",
  "RadioGroup",
  "CheckboxGroup",
  "Form",
  "ListViewItem",
]

/** Each component's description and props schema, without how it's drawn. */
export const RICH_ANSWER_VOCABULARY: Record<
  RichAnswerComponent,
  { description: string; properties: z.ZodType }
> = Object.fromEntries(
  RICH_ANSWER_COMPONENTS.map((name) => {
    const entry =
      name === "Choices" ? Choices : defaultGenerativeUILibrary[name]
    if (!entry) throw new Error(`The package has no ${name} component.`)
    return [
      name,
      { description: entry.description, properties: entry.properties },
    ]
  })
) as Record<RichAnswerComponent, { description: string; properties: z.ZodType }>

/** A picked list made whole: known components, what they need, in order. */
export function withRequired(picked: readonly string[]): RichAnswerComponent[] {
  const chosen = new Set<string>()
  const add = (name: string) => {
    if (chosen.has(name) || !RICH_ANSWER_COMPONENTS.includes(name as never))
      return
    chosen.add(name)
    for (const needed of RICH_ANSWER_REQUIRES[name as RichAnswerComponent] ??
      [])
      add(needed)
  }
  picked.forEach(add)
  return RICH_ANSWER_COMPONENTS.filter((name) => chosen.has(name))
}

type Schema = ReturnType<typeof buildPresentParameters>

/** One component's props as the `present` schema has them. */
function propsOf(name: RichAnswerComponent): Record<string, Schema> {
  const entry = RICH_ANSWER_VOCABULARY[name]
  const built = buildPresentParameters({
    [name]: { ...entry, render: () => null },
  } as GenerativeUILibrary)
  const extra = Object.keys(built.$defs ?? {}).filter(
    (key) => key !== "node" && key !== "children"
  )
  if (extra.length) {
    // The runtime merges components' props as they are: none may point
    // into $defs of its own.
    throw new Error(`${name}'s props refer to ${extra.join(", ")}.`)
  }
  return Object.fromEntries(
    Object.entries(built.properties ?? {}).filter(
      ([key]) => !["_type", "_key", "children"].includes(key)
    )
  ) as Record<string, Schema>
}

/**
 * What an `_action` is, for the components that act: a reply, the text the
 * person sends with what they chose or entered.
 */
const ACTION = {
  type: "object",
  description:
    'What using this component does: {"type": "reply", "text": "..."} sends text to you as the person\'s next message, followed by what they chose or entered. On a Form, or on a Card\'s confirm, it sends every named control\'s value.',
  properties: {
    type: { type: "string", enum: ["reply"] },
    text: {
      type: "string",
      description:
        "What the person says by using it, before their value; e.g. 'Approve' or 'Region:'.",
    },
  },
  required: ["type"],
}

/** What a node's `children` are: always a list, of components or text. */
const CHILDREN = {
  description: "What's inside this component, in order: components or text.",
  type: "array",
  items: { anyOf: [{ type: "string" }, { $ref: "#/$defs/node" }] },
}

/**
 * The catalog the agent runtime builds a `present` schema from, for the
 * components an agent picked: each component's description and its own
 * props, merged as the package merges them (one flat node, `_type` naming
 * the component, a shared prop's distinct schemas as `anyOf`).
 *
 * The schema it builds is the package's made cheaper for the models we run
 * on: no `_key`, and `children` always a list. Gemini writes the recursion
 * out into the prompt, so every branch costs; it also refuses recursion
 * through the root (`"$ref": "#"`), so it stays in `$defs`.
 */
export function presentCatalog() {
  return {
    groups: RICH_ANSWER_GROUPS.map((group) => ({
      id: group.id,
      label: group.label,
      components: [...group.components],
    })),
    defaults: [...DEFAULT_RICH_ANSWER_COMPONENTS],
    requires: RICH_ANSWER_REQUIRES,
    acting: [...RICH_ANSWER_ACTING],
    action: ACTION,
    children: CHILDREN,
    components: Object.fromEntries(
      RICH_ANSWER_COMPONENTS.map((name) => [
        name,
        {
          description: RICH_ANSWER_VOCABULARY[name].description,
          properties: propsOf(name),
        },
      ])
    ),
  }
}

/**
 * About how many prompt tokens the picked components add to every model
 * call, on OpenAI and on Gemini: their schema's JSON (at four characters a
 * token) times what each provider was measured to count for it (gpt-5.2
 * about three quarters, Gemini 3.5 Flash, which writes the nesting out,
 * about 2.2 times; 2026-10-14, from 8 to 36 components). A guide, not a
 * bill.
 */
export function schemaCost(picked: readonly string[]): {
  openai: number
  gemini: number
} {
  const names = withRequired(picked)
  if (!names.length) return { openai: 0, gemini: 0 }
  const size = names.reduce(
    (total, name) =>
      total +
      RICH_ANSWER_VOCABULARY[name].description.length +
      JSON.stringify(propsOf(name)).length,
    // The node's frame, `children` and the tool's own words.
    1200 +
      (names.some((name) => RICH_ANSWER_ACTING.includes(name))
        ? JSON.stringify(ACTION).length
        : 0)
  )
  const round = (tokens: number) => Math.max(50, Math.round(tokens / 50) * 50)
  return { openai: round((size / 4) * 0.75), gemini: round((size / 4) * 2.2) }
}

/* Choices ------------------------------------------------------------------ */

/** A choice as the chat shows it: what it says, and what clicking it sends. */
export type Choice = { label: string; reply: string }

/** The options of a Choices node that can be shown, as the model wrote them. */
export const choicesOf = (options: unknown): Choice[] =>
  (Array.isArray(options) ? options : [])
    .flatMap((option: { label?: unknown; reply?: unknown } | undefined) => {
      const label = typeof option?.label === "string" ? option.label.trim() : ""
      if (!label) return []
      const reply =
        typeof option?.reply === "string" && option.reply.trim()
          ? option.reply.trim()
          : label
      return [{ label, reply }]
    })
    .slice(0, 8)

/** A message part, as much of it as these read. */
export type MessagePart = {
  type: string
  text?: string
  toolName?: string
  args?: unknown
  result?: unknown
}

/**
 * What the agent server said of a `present` call: true when it took it,
 * false when it refused it (the model sends it again), undefined before it
 * answered. ADK hands results over as JSON text or values, sometimes
 * wrapped as `{ result }`.
 */
export function shownOf(result: unknown): boolean | undefined {
  let value = result
  if (typeof value === "string") {
    try {
      value = JSON.parse(value)
    } catch {
      return undefined
    }
  }
  if (typeof value === "object" && value !== null && "result" in value) {
    value = (value as { result: unknown }).result
  }
  if (typeof value !== "object" || value === null) return undefined
  const shown = (value as { shown?: unknown }).shown
  return typeof shown === "boolean" ? shown : undefined
}

/** Whether a tree has a Choices node with an option, a few levels down. */
function hasChoices(node: unknown, depth = 0): boolean {
  if (depth > 8 || typeof node !== "object" || node === null) return false
  if (Array.isArray(node)) return node.some((n) => hasChoices(n, depth + 1))
  const element = node as {
    _type?: unknown
    children?: unknown
    options?: unknown
  }
  if (element._type === "Choices" && choicesOf(element.options).length)
    return true
  return hasChoices(element.children, depth + 1)
}

/**
 * Whether a message's rich answer offers Choices, which the chat's
 * follow-up chips would only repeat.
 */
export function offersChoices(
  message: { role: string; content: readonly MessagePart[] } | undefined
) {
  return (
    message?.role === "assistant" &&
    message.content.some(
      (part) =>
        part.type === "tool-call" &&
        part.toolName === "present" &&
        shownOf(part.result) !== false &&
        hasChoices(part.args)
    )
  )
}

/* Forms -------------------------------------------------------------------- */

/** Controls that are a form's fields: inside one, its confirm sends them. */
const FIELDS = new Set([
  "Select",
  "Input",
  "DatePicker",
  "Checkbox",
  "Slider",
  "RadioGroup",
  "CheckboxGroup",
])

/**
 * A composition whose forms' fields don't act on their own: inside a Form,
 * or a Card with asForm, a field's `_action` is dropped, so choosing a date
 * doesn't send it before the rest is filled in; the form's confirm sends
 * every field together. Buttons keep theirs. Models add them anyway.
 */
export function withQuietFields(
  node: unknown,
  inForm = false,
  depth = 0
): unknown {
  if (depth > 32) return node
  if (Array.isArray(node))
    return node.map((each) => withQuietFields(each, inForm, depth + 1))
  if (typeof node !== "object" || node === null) return node
  const element = node as Record<string, unknown>
  const type = element._type ?? element.$type
  const form =
    inForm || type === "Form" || (type === "Card" && element.asForm === true)
  let kept = element
  if (inForm && FIELDS.has(String(type))) {
    kept = Object.fromEntries(
      Object.entries(element).filter(
        ([key]) => key !== "_action" && key !== "$action"
      )
    )
  }
  return "children" in kept
    ? { ...kept, children: withQuietFields(kept.children, form, depth + 1) }
    : kept
}

/* Replies ------------------------------------------------------------------ */

const valueText = (value: unknown): string => {
  if (value === undefined || value === null) return ""
  if (typeof value === "boolean") return value ? "Yes" : "No"
  if (Array.isArray(value))
    return value.map(valueText).filter(Boolean).join(", ")
  if (typeof value === "object") {
    return Object.entries(value as Record<string, unknown>)
      .map(([key, each]) => [key, valueText(each)])
      .filter(([, each]) => each)
      .map(([key, each]) => `- ${key}: ${each}`)
      .join("\n")
  }
  return String(value).trim()
}

/**
 * The person's message for an action a component fired: its `text`, then
 * what they chose or entered (`$input`: a value, or a form's values by
 * name); an action without text sends its type in words.
 */
export function replyOf(action: unknown): string {
  if (typeof action !== "object" || action === null) return ""
  const { text, type, $input } = action as Record<string, unknown>
  const said =
    typeof text === "string" && text.trim()
      ? text.trim()
      : typeof type === "string" && type !== "reply"
        ? type.replace(/[_-]+/g, " ").trim()
        : ""
  const value = valueText($input)
  if (!value) return said
  if (!said) return value
  return value.includes("\n") ? `${said}\n${value}` : `${said} ${value}`
}
