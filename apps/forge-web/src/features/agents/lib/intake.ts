import type { IssueLevel } from "@/features/builder/lib/types"
import { checkField } from "@/features/steps/lib/expressions"
import type { Root, Scope } from "@/features/steps/lib/scope"
import { t, type DataType } from "@/features/steps/lib/types"
import type {
  ChatConfigs,
  IntakeField,
  IntakeFieldType,
  IntakeSection,
} from "./model"

/*
 * An intake agent's design (forge_agent_runtime.intake): the record it
 * collects, as fields in sections, and the checks the builder shows on it.
 * The runtime decides what to ask next from the same design; the builder's
 * preview asks the admin API what it would (`previewIntake`), rather than
 * working it out again here.
 */

/** The types a field can have, in the menu's order. */
export const INTAKE_FIELD_TYPES = [
  "text",
  "number",
  "integer",
  "boolean",
  "date",
  "email",
  "choice",
  "choices",
] as const satisfies readonly IntakeFieldType[]

/** What each type is called where it's picked. */
export const FIELD_TYPE_LABELS: Record<IntakeFieldType, string> = {
  text: "Text",
  number: "Number",
  integer: "Whole number",
  boolean: "Yes or no",
  date: "Date",
  email: "Email address",
  choice: "One of a list",
  choices: "Any of a list",
}

/** A field's key: what the record and the model call it. */
export const INTAKE_KEY_PATTERN = /^[a-z][a-z0-9_]{0,63}$/

/** What an agent handing off says besides the fields: no field may be called it. */
export const RESERVED_INTAKE_KEYS = new Set(["background"])

/** The most fields and sections an intake may have, as the runtime allows. */
export const MOST_FIELDS = 200
export const MOST_SECTIONS = 50

export const isChoice = (type: IntakeFieldType) =>
  type === "choice" || type === "choices"

/** A field's name as people read it: its label, else its key. */
export const fieldTitle = (field: IntakeField) =>
  field.label.trim() || field.key || "Untitled field"

/** A label as a key: "Date of birth" → `date_of_birth`, apart from `taken`. */
export function keyFromLabel(label: string, taken: Iterable<string> = []) {
  const used = new Set(taken)
  let base = label
    .normalize("NFKD")
    .replace(/[̀-ͯ]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "")
    .slice(0, 48)
  if (!base) base = "field"
  if (!/^[a-z]/.test(base)) base = `f_${base}`
  if (RESERVED_INTAKE_KEYS.has(base)) base = `${base}_info`
  if (!used.has(base)) return base
  for (let n = 2; ; n += 1) {
    if (!used.has(`${base}_${n}`)) return `${base}_${n}`
  }
}

const ID_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"

function newId(prefix: string, taken: Iterable<string>) {
  const used = new Set(taken)
  for (;;) {
    const random = crypto.getRandomValues(new Uint8Array(5))
    const id = `${prefix}_${Array.from(random, (b) => ID_ALPHABET[b % ID_ALPHABET.length]).join("")}`
    if (!used.has(id)) return id
  }
}

/** A new field, at the end of a section (or of the fields in none). */
export function newIntakeField(
  fields: IntakeField[],
  section = "",
  label = "New field"
): IntakeField {
  return {
    id: newId(
      "f",
      fields.map((f) => f.id)
    ),
    key: keyFromLabel(
      label,
      fields.map((f) => f.key)
    ),
    label,
    type: "text",
    options: [],
    description: "",
    why: "",
    required: true,
    when: "",
    infer: false,
    confirm: false,
    prefill: false,
    section,
  }
}

/** A new section, at the end. */
export function newIntakeSection(sections: IntakeSection[]): IntakeSection {
  return {
    id: newId(
      "s",
      sections.map((s) => s.id)
    ),
    title: `Section ${sections.length + 1}`,
    when: "",
  }
}

/** Fields by section, in the design's order: those in none first. */
export function fieldGroups(
  fields: IntakeField[],
  sections: IntakeSection[]
): { section: IntakeSection | null; fields: IntakeField[] }[] {
  const known = new Set(sections.map((s) => s.id))
  const loose = fields.filter((f) => !known.has(f.section))
  return [
    ...(loose.length || !sections.length
      ? [{ section: null, fields: loose }]
      : []),
    ...sections.map((section) => ({
      section,
      fields: fields.filter((f) => f.section === section.id),
    })),
  ]
}

/** A field's value as conditions read it. */
export function fieldType(field: IntakeField): DataType {
  const about = field.description.trim() || fieldTitle(field)
  switch (field.type) {
    case "number":
    case "integer":
      return t.number(about)
    case "boolean":
      return t.boolean(about)
    case "choice":
      return t.string(about, {
        enum: field.options.map((o) => o.trim()).filter(Boolean),
      })
    case "choices":
      return t.array(
        t.string("One of its options.", {
          enum: field.options.map((o) => o.trim()).filter(Boolean),
        }),
        about
      )
    default:
      return t.string(about)
  }
}

/**
 * What a condition reads: the record's values, each field by its key, as
 * the runtime evaluates it (only true holds; a field with no value yet is
 * missing).
 */
export function intakeScope(fields: IntakeField[]): Scope {
  const roots: Root[] = fields
    .filter((f) => INTAKE_KEY_PATTERN.test(f.key))
    .map((field) => ({
      name: field.key,
      detail: fieldTitle(field),
      type: fieldType(field),
    }))
  return {
    roots: new Map(roots.map((root) => [root.name, root])),
    steps: new Map(),
    allSteps: new Map(),
  }
}

/** Where an issue about one of a field's settings points: its card's setting. */
export const fieldSetting = (field: IntakeField, setting: string) =>
  `fields.${field.id}.${setting}`

/** Where an issue about one of a section's settings points. */
export const sectionSetting = (section: IntakeSection, setting: string) =>
  `sections.${section.id}.${setting}`

type Found = [code: string, level: IssueLevel, message: string, field?: string]

/** What's wrong with an intake's design: what the runtime would refuse, and what's likely a slip. */
export function intakeIssues(config: ChatConfigs["intake_agent"]): Found[] {
  const out: Found[] = []
  const { fields, sections } = config
  if (!fields.length) {
    out.push(["no-fields", "error", "Add the fields it collects.", "fields"])
  }
  if (fields.length > MOST_FIELDS || sections.length > MOST_SECTIONS) {
    out.push([
      "too-many",
      "error",
      `An intake has at most ${MOST_FIELDS} fields and ${MOST_SECTIONS} sections.`,
      "fields",
    ])
  }
  const scope = intakeScope(fields)
  const sectionIds = new Set(sections.map((s) => s.id))
  const keys = new Map<string, number>()
  for (const field of fields)
    keys.set(field.key, (keys.get(field.key) ?? 0) + 1)

  for (const field of fields) {
    const title = fieldTitle(field)
    if (!INTAKE_KEY_PATTERN.test(field.key)) {
      out.push([
        `key:${field.id}`,
        "error",
        `${title}: its key is lower-case letters, digits and _, starting with a letter.`,
        fieldSetting(field, "key"),
      ])
    } else if (RESERVED_INTAKE_KEYS.has(field.key)) {
      out.push([
        `key:${field.id}`,
        "error",
        `${title}: “${field.key}” is kept for what the agent handing off says.`,
        fieldSetting(field, "key"),
      ])
    } else if ((keys.get(field.key) ?? 0) > 1) {
      out.push([
        `key:${field.id}`,
        "error",
        `Two fields are called ${field.key}; every key must be different.`,
        fieldSetting(field, "key"),
      ])
    }
    if (isChoice(field.type)) {
      const options = field.options.map((o) => o.trim())
      if (!options.some(Boolean)) {
        out.push([
          `options:${field.id}`,
          "error",
          `${title}: give the choice its options.`,
          fieldSetting(field, "options"),
        ])
      } else if (
        options.some((o) => !o) ||
        new Set(options.map((o) => o.toLowerCase())).size !== options.length
      ) {
        out.push([
          `options:${field.id}`,
          "error",
          `${title}: an option is empty or there twice.`,
          fieldSetting(field, "options"),
        ])
      }
    }
    if (field.section && !sectionIds.has(field.section)) {
      out.push([
        `section:${field.id}`,
        "error",
        `${title} is in a section that's gone: pick another.`,
        fieldSetting(field, "section"),
      ])
    }
    if (field.when.trim()) {
      checkField(field.when, "expression", scope, { expect: "condition" })
        .filter((d) => d.severity === "error")
        .slice(0, 1)
        .forEach((d) =>
          out.push([
            // ":field:": its field says it already (InstructionField's way).
            `when:field:${field.id}`,
            "error",
            `${title}, applies when: ${d.message}`,
            fieldSetting(field, "when"),
          ])
        )
    }
  }
  for (const section of sections) {
    if (!section.when.trim()) continue
    checkField(section.when, "expression", scope, { expect: "condition" })
      .filter((d) => d.severity === "error")
      .slice(0, 1)
      .forEach((d) =>
        out.push([
          `section-when:field:${section.id}`,
          "error",
          `${section.title || "A section"}, unlocks when: ${d.message}`,
          sectionSetting(section, "when"),
        ])
      )
  }
  for (const section of sections) {
    if (!fields.some((f) => f.section === section.id)) {
      out.push([
        `empty-section:${section.id}`,
        "warning",
        `${section.title || "A section"} has no fields.`,
        sectionSetting(section, "title"),
      ])
    }
  }
  return out
}

/* -------------------------------------------------------------------------- */
/* The preview                                                                */
/* -------------------------------------------------------------------------- */

/** How a sample answer came to be in the record. */
export type AnswerHow =
  "stated" | "inferred" | "handoff" | "confirmed" | "skipped"

/** A sample answer, as the preview sends it. */
export type SampleAnswer = { value: unknown; how: AnswerHow }

/** What `POST …/chat-agents/intake-preview` answers. */
export type IntakePreview = {
  /** What the runtime would refuse in the design. */
  problems: string[]
  /** Sample answers a field doesn't take, and why. */
  refused?: Record<string, string>
  applying?: string[]
  missing?: string[]
  unconfirmed?: string[]
  optional?: string[]
  /** Sections whose condition doesn't hold, by ID. */
  locked?: string[]
  ask_next?: string[]
  next_section?: string | null
  /** Every required field is in, and confirmed where it must be. */
  ready?: boolean
  /** It may finish as completed. */
  done?: boolean
  /** What its model would be told before its next call. */
  brief?: string
  /** What it would hand back, finished now. */
  result?: {
    status: string
    note: string
    record: Record<string, unknown>
    missing: string[]
  }
  /** What an agent hands off to it with: a JSON Schema. */
  input_schema?: Record<string, unknown> | null
}

/**
 * A sample answer's text as a value of its field's type, for the preview:
 * the runtime checks it as it would the model's.
 */
export function sampleValue(field: IntakeField, text: string): unknown {
  if (field.type === "choices")
    return text
      .split(",")
      .map((v) => v.trim())
      .filter(Boolean)
  return text
}
