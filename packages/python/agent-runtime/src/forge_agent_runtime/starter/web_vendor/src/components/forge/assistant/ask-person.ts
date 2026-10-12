/**
 * An agent's question (ADK's `adk_request_input`) as a form: the questions
 * its `response_schema` asks, one per field of an object schema or one for
 * any other, and the answer read back from the form's data in the shape and
 * types the schema declares, as ADK checks it when the turn carries on.
 */

/** A JSON Schema, as far as a form for it reads one. */
export type AnswerSchema = {
  type?: string | string[]
  enum?: unknown[]
  items?: AnswerSchema
  properties?: Record<string, AnswerSchema>
  required?: string[]
  title?: string
  description?: string
  format?: string
  default?: unknown
  minimum?: number
  maximum?: number
}

/** An answer: what the schema describes, as JSON. */
export type Answer =
  string | number | boolean | null | Answer[] | { [key: string]: Answer }

/** The input types the form's free answers take. */
export type AnswerInputType =
  "text" | "number" | "email" | "url" | "date" | "datetime-local" | "time"

/** How an answer is read back: its JSON type. */
type Reading =
  "string" | "number" | "integer" | "boolean" | "list" | "json" | "choice"

export type Question = {
  /** Its name in the form's data: the field's, or `answer`. */
  name: string
  /** Shown as the question. */
  title: string
  description?: string
  required: boolean
  /** Pick one, pick several, or a free answer in an input. */
  kind: "single" | "multiple" | "input"
  choices?: { value: string; label: string }[]
  input?: {
    type: AnswerInputType
    placeholder?: string
    min?: number
    max?: number
    step?: number | "any"
  }
  /** What's filled in or picked before the person answers. */
  defaults: string[]
  reading: Reading
  /** A choice's value as the schema has it, by the form's value. */
  values?: Record<string, Answer>
}

export type AskForm = {
  questions: Question[]
  /** The answer is an object of the questions' answers; else the one answer. */
  fields: boolean
}

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value)

/** A schema's type, the first non-null when it lists several. */
function typeOf(schema: AnswerSchema): string | undefined {
  const type = schema.type
  if (Array.isArray(type)) return type.find((each) => each !== "null")
  return type
}

/** "dueDate" / "due_date" → "Due date". */
export function humanize(name: string) {
  const words = name
    .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
    .replace(/[_-]+/g, " ")
    .trim()
    .toLowerCase()
  return words ? words[0]!.toUpperCase() + words.slice(1) : name
}

const INPUT_FORMATS: Record<string, AnswerInputType> = {
  email: "email",
  uri: "url",
  url: "url",
  date: "date",
  "date-time": "datetime-local",
  time: "time",
}

const defaultsOf = (value: unknown): string[] =>
  value === undefined || value === null
    ? []
    : Array.isArray(value)
      ? value.map(String)
      : [isRecord(value) ? JSON.stringify(value) : String(value)]

/** One question for a schema: a field's, or the whole answer's. */
function question(
  name: string,
  raw: unknown,
  title: string,
  required: boolean
): Question {
  const schema: AnswerSchema = isRecord(raw) ? raw : {}
  const type = typeOf(schema)
  const base = {
    name,
    title: schema.title || title,
    ...(schema.description && { description: schema.description }),
    required,
    defaults: defaultsOf(schema.default),
  }
  if (schema.enum?.length) {
    const values = Object.fromEntries(
      schema.enum.map((value) => [String(value), value as Answer])
    )
    return {
      ...base,
      kind: "single",
      choices: Object.keys(values).map((value) => ({ value, label: value })),
      reading: "choice",
      values,
    }
  }
  if (type === "boolean") {
    return {
      ...base,
      kind: "single",
      choices: [
        { value: "true", label: "Yes" },
        { value: "false", label: "No" },
      ],
      reading: "boolean",
    }
  }
  if (type === "array" && isRecord(schema.items) && schema.items.enum?.length) {
    const values = Object.fromEntries(
      schema.items.enum.map((value) => [String(value), value as Answer])
    )
    return {
      ...base,
      kind: "multiple",
      choices: Object.keys(values).map((value) => ({ value, label: value })),
      reading: "choice",
      values,
    }
  }
  if (type === "number" || type === "integer") {
    return {
      ...base,
      kind: "input",
      input: {
        type: "number",
        ...(schema.minimum !== undefined && { min: schema.minimum }),
        ...(schema.maximum !== undefined && { max: schema.maximum }),
        step: type === "integer" ? 1 : "any",
      },
      reading: type,
    }
  }
  if (type === "array") {
    return {
      ...base,
      kind: "input",
      input: { type: "text", placeholder: "Separate them with commas" },
      reading: "list",
    }
  }
  if (
    type === "object" ||
    (type === undefined && isRecord(schema.properties))
  ) {
    return {
      ...base,
      kind: "input",
      input: { type: "text", placeholder: "JSON" },
      reading: "json",
    }
  }
  return {
    ...base,
    kind: "input",
    input: {
      type: (schema.format && INPUT_FORMATS[schema.format]) || "text",
      placeholder: "Your answer",
    },
    reading: "string",
  }
}

/**
 * The form for an agent's question: a question per field of an object
 * schema (its `required` ones required), or one for any other schema, the
 * agent's `message` its title. No schema asks for text.
 */
export function askForm(schema: unknown, message: string): AskForm {
  const fields = isRecord(schema) ? schema.properties : undefined
  if (
    isRecord(schema) &&
    isRecord(fields) &&
    Object.keys(fields).length > 0 &&
    (typeOf(schema) ?? "object") === "object"
  ) {
    const required = new Set(
      Array.isArray(schema.required) ? schema.required.map(String) : []
    )
    return {
      fields: true,
      questions: Object.entries(fields).map(([name, field]) =>
        question(name, field, humanize(name), required.has(name))
      ),
    }
  }
  return {
    fields: false,
    questions: [question("answer", schema, message, true)],
  }
}

/** What the form's data holds: `FormData` or anything with its two reads. */
export type FormValues = {
  get(name: string): FormDataEntryValue | null
  getAll(name: string): FormDataEntryValue[]
}

const NOTHING = Symbol("nothing")

/** One question's answer in the schema's type; NOTHING when it's left empty. */
function read(question: Question, values: FormValues): Answer | typeof NOTHING {
  if (question.kind === "multiple") {
    const picked = values.getAll(question.name).map(String)
    if (!picked.length) return question.required ? [] : NOTHING
    return picked.map((value) => question.values?.[value] ?? value)
  }
  const raw = values.get(question.name)
  const text = typeof raw === "string" ? raw.trim() : ""
  if (!text) return NOTHING
  switch (question.reading) {
    case "choice":
      return question.values && text in question.values
        ? question.values[text]
        : text
    case "boolean":
      return text === "true"
    case "number": {
      const number = Number(text)
      return Number.isFinite(number) ? number : text
    }
    case "integer": {
      const number = Number(text)
      return Number.isInteger(number) ? number : text
    }
    case "list":
      return text
        .split(",")
        .map((each) => each.trim())
        .filter(Boolean)
    case "json":
      try {
        return JSON.parse(text) as Answer
      } catch {
        return text
      }
    default:
      return text
  }
}

/**
 * The answer to send: an object of the fields answered (those left empty
 * left out), or the one answer.
 */
export function answerFrom(form: AskForm, values: FormValues): Answer {
  if (!form.fields) {
    const answer = read(form.questions[0]!, values)
    return answer === NOTHING ? "" : answer
  }
  const answer: Record<string, Answer> = {}
  for (const question of form.questions) {
    const value = read(question, values)
    if (value !== NOTHING) answer[question.name] = value
  }
  return answer
}

const shown = (value: unknown): string =>
  value === true
    ? "Yes"
    : value === false
      ? "No"
      : Array.isArray(value)
        ? value.map(shown).join(", ")
        : isRecord(value)
          ? JSON.stringify(value)
          : String(value)

/** An answer in a line: "Environment: staging · Notify: Yes", or the answer itself. */
export function describeAnswer(answer: unknown, form: AskForm): string {
  if (form.fields && isRecord(answer)) {
    const titles = new Map(form.questions.map((q) => [q.name, q.title]))
    const parts = Object.entries(answer).map(([name, value]) => {
      const title = titles.get(name) ?? humanize(name)
      // A question needs no colon after its question mark.
      return `${title}${title.endsWith("?") ? "" : ":"} ${shown(value)}`
    })
    return parts.length ? parts.join(" · ") : "Nothing filled in"
  }
  return shown(answer)
}
