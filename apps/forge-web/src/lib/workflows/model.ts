import {
  Calendar03Icon,
  CursorPointer01Icon,
  Flag02Icon,
  FunctionIcon,
  GitForkIcon,
  GitMergeIcon,
  Globe02Icon,
  HourglassIcon,
  LeftToRightListBulletIcon,
  PlayIcon,
  RepeatIcon,
  Robot01Icon,
  Route02Icon,
  UserCheck01Icon,
  WebhookIcon,
  WorkflowSquare03Icon,
} from "@hugeicons/core-free-icons"

import type { IconProp } from "@/components/forge/icons"
import type { StepOutput } from "@/lib/builder/types"

/*
 * An organization's workflow: steps (the graph's nodes) joined by connections (its
 * edges), run from one entry point. Each step is a kind from the catalog
 * below, with that kind's settings (`config`) and the outputs it can leave
 * by: one `next` for most steps, a named output per branch for the logic
 * steps. A connection leaves a step by one of its outputs and enters
 * another step. The builder edits this model; `document.ts` writes it as
 * the workflow's JSON and reads it back.
 */

export const WORKFLOWS_ICON: IconProp = WorkflowSquare03Icon

/* -------------------------------------------------------------------------- */
/* Settings, per kind                                                         */
/* -------------------------------------------------------------------------- */

export type TriggerKind = "manual" | "event" | "schedule"
export type HttpMethod = "GET" | "POST" | "PUT" | "PATCH" | "DELETE"
export type DelayUnit = "seconds" | "minutes" | "hours" | "days"

/** A header of an HTTP request. */
export type HeaderRow = { id: string; name: string; value: string }
/** A case of a switch: the value that sends the run its way. */
export type SwitchCase = { id: string; value: string }
/** A rule of a match: its label and the condition that takes it. */
export type MatchArm = { id: string; label: string; condition: string }

export type StepConfigs = {
  entry: {
    trigger: TriggerKind
    /** With an `event` trigger: the keys of the event types that start it. */
    event_types: string[]
    /** With a `schedule` trigger: when, as a cron expression. */
    cron: string
    timezone: string
    /**
     * With a `manual` trigger: the input whoever starts it gives, as a JSON
     * Schema. Empty takes anything (and later steps can't be checked
     * against it).
     */
    input_schema: Record<string, unknown>
  }
  agent: {
    instructions: string
    /**
     * One of the models Forge offers (model_provider.yaml, as the assistant
     * lists them): its provider's key and its id. Both empty: the default.
     */
    model: { provider: string; name: string }
    /** How long the model thinks: the nearest level it offers; empty for its own. */
    thinking_level: ThinkingLevel | ""
    /** `json` returns an object matching `output_schema`. */
    output: "text" | "json"
    output_schema: string
  }
  approval: {
    message: string
    /** Who may decide: an organization role. */
    approvers: "org:admin" | "org:member"
    /** Hours to wait before the step takes Rejected; 0 waits forever. */
    timeout_hours: number
  }
  http: {
    method: HttpMethod
    url: string
    headers: HeaderRow[]
    /** A JSONata expression making the body: an object or list goes as JSON. */
    body: string
    timeout_seconds: number
    retries: number
    /** A JSON Schema of the response's body; `{}` leaves it undeclared. */
    output_schema: Record<string, unknown>
  }
  transform: {
    /** A JSONata expression: what it hands on. */
    expression: string
    /** A JSON Schema of what it makes; `{}` leaves it undeclared. */
    output_schema: Record<string, unknown>
  }
  delay: { amount: number; unit: DelayUnit }
  subworkflow: {
    /** Another of the organization's workflows, by ID. */
    workflow: string
    /** Wait for it to finish and take its result; else start it and go on. */
    wait: boolean
  }
  if: { condition: string }
  switch: { value: string; cases: SwitchCase[] }
  match: { arms: MatchArm[] }
  loop: {
    /** An expression giving the list to go through. */
    items: string
    /** What the body calls the current item. */
    item_name: string
    max_iterations: number
    /** How many items run at once; 1 runs them in order. */
    concurrency: number
  }
  merge: { mode: "all" | "any" }
  end: {
    outcome: "succeeded" | "failed"
    /** An expression giving the workflow's result. */
    result: string
  }
}

export type StepKind = keyof StepConfigs

/** A step as the builder holds it: the canvas node's data. */
export type StepData<K extends StepKind = StepKind> = {
  [Kind in K]: { kind: Kind; name: string; config: StepConfigs[Kind] }
}[K]

/* -------------------------------------------------------------------------- */
/* Outputs                                                                    */
/* -------------------------------------------------------------------------- */

export { INPUT, type StepOutput } from "@/lib/builder/types"

const NEXT: StepOutput[] = [{ id: "next", label: "Next" }]

/** A step's ways out, in the order the canvas lists them. */
export function outputsOf(step: StepData): StepOutput[] {
  switch (step.kind) {
    case "end":
      return []
    case "http":
      return [
        { id: "success", label: "Success", branch: true },
        { id: "error", label: "Error", branch: true, fallback: true },
      ]
    case "approval":
      return [
        { id: "approved", label: "Approved", branch: true },
        { id: "rejected", label: "Rejected", branch: true, fallback: true },
      ]
    case "if":
      return [
        { id: "true", label: "True", branch: true },
        { id: "false", label: "False", branch: true, fallback: true },
      ]
    case "switch":
      return [
        ...step.config.cases.map((c) => ({
          id: c.id,
          label: c.value.trim() || "Empty value",
          branch: true,
        })),
        { id: "default", label: "Default", branch: true, fallback: true },
      ]
    case "match":
      return [
        ...step.config.arms.map((arm, index) => ({
          id: arm.id,
          label: arm.label.trim() || `Rule ${index + 1}`,
          branch: true,
        })),
        { id: "otherwise", label: "Otherwise", branch: true, fallback: true },
      ]
    case "loop":
      return [
        { id: "each", label: "Each item", branch: true, body: true },
        { id: "done", label: "Done", branch: true },
      ]
    default:
      return NEXT
  }
}

/* -------------------------------------------------------------------------- */
/* Catalog                                                                    */
/* -------------------------------------------------------------------------- */

export type StepGroup = "start" | "agents" | "actions" | "logic" | "finish"

export const STEP_GROUPS: { id: StepGroup; label: string }[] = [
  { id: "start", label: "Start" },
  { id: "agents", label: "Agents & people" },
  { id: "actions", label: "Actions" },
  { id: "logic", label: "Logic" },
  { id: "finish", label: "Finish" },
]

type KindInfo<K extends StepKind> = {
  label: string
  group: StepGroup
  icon: IconProp
  /** One line: what the step does. */
  summary: string
  /** Words the library's filter also matches. */
  keywords: string
  /** Agent steps wear an agent orb: which of the five. */
  orb?: number
  /** Drawn as a start or finish: an ink tile. */
  terminal?: boolean
  /** A person decides here: the warm notice surface. */
  person?: boolean
  /** The prefix of a new step's ID. */
  idPrefix: string
  defaults: () => StepConfigs[K]
}

let uidCounter = 0
/** A short ID for a case, rule, route or header. Unique within the page. */
export function uid(prefix: string) {
  uidCounter += 1
  return `${prefix}_${Date.now().toString(36).slice(-4)}${uidCounter.toString(36)}`
}

export const STEP_KINDS: { [K in StepKind]: KindInfo<K> } = {
  entry: {
    label: "Start",
    group: "start",
    icon: PlayIcon,
    summary: "Where every run starts: the values it needs to begin.",
    keywords: "start input schema fields initial seed values begin",
    terminal: true,
    idPrefix: "start",
    defaults: () => ({
      trigger: "manual",
      event_types: [],
      cron: "0 9 * * 1-5",
      timezone: "UTC",
      input_schema: {},
    }),
  },
  agent: {
    label: "Agent",
    group: "agents",
    icon: Robot01Icon,
    summary: "An agent works on the run's data with your instructions.",
    keywords: "ai llm model prompt assistant classify summarize",
    orb: 0,
    idPrefix: "agent",
    defaults: () => ({
      instructions: "",
      model: { provider: "", name: "" },
      thinking_level: "",
      output: "text",
      output_schema: "",
    }),
  },
  approval: {
    label: "Approval",
    group: "agents",
    icon: UserCheck01Icon,
    summary: "The run waits for a person to approve or reject.",
    keywords: "human review person approve reject handoff sign off",
    person: true,
    idPrefix: "approval",
    defaults: () => ({ message: "", approvers: "org:admin", timeout_hours: 0 }),
  },
  http: {
    label: "HTTP request",
    group: "actions",
    icon: Globe02Icon,
    summary: "Calls a URL and hands on the response.",
    keywords: "api rest fetch call url post get webhook",
    idPrefix: "http",
    defaults: () => ({
      method: "GET",
      url: "",
      headers: [],
      body: "",
      timeout_seconds: 30,
      retries: 0,
      output_schema: {},
    }),
  },
  transform: {
    label: "Transform",
    group: "actions",
    icon: FunctionIcon,
    summary: "Reshapes data with a JSONata expression.",
    keywords: "map reshape jsonata expression object data",
    idPrefix: "transform",
    defaults: () => ({ expression: "", output_schema: {} }),
  },
  delay: {
    label: "Delay",
    group: "actions",
    icon: HourglassIcon,
    summary: "Waits a while before going on.",
    keywords: "wait sleep pause timer",
    idPrefix: "delay",
    defaults: () => ({ amount: 5, unit: "minutes" }),
  },
  subworkflow: {
    label: "Run workflow",
    group: "actions",
    icon: WorkflowSquare03Icon,
    summary: "Runs another of the organization's workflows.",
    keywords: "subworkflow call nested child",
    idPrefix: "run_workflow",
    defaults: () => ({ workflow: "", wait: true }),
  },
  if: {
    label: "If / else",
    group: "logic",
    icon: GitForkIcon,
    summary: "Takes one of two ways, on a condition.",
    keywords: "condition branch boolean true false",
    idPrefix: "if",
    defaults: () => ({ condition: "" }),
  },
  switch: {
    label: "Switch",
    group: "logic",
    icon: Route02Icon,
    summary: "Routes on one value: to the case it equals.",
    keywords: "case value equals enum route branch",
    idPrefix: "switch",
    defaults: () => ({
      value: "",
      cases: [
        { id: uid("case"), value: "" },
        { id: uid("case"), value: "" },
      ],
    }),
  },
  match: {
    label: "Match",
    group: "logic",
    icon: LeftToRightListBulletIcon,
    summary: "Routes to the first rule whose condition holds.",
    keywords: "pattern rules guard first condition route",
    idPrefix: "match",
    defaults: () => ({
      arms: [
        { id: uid("rule"), label: "", condition: "" },
        { id: uid("rule"), label: "", condition: "" },
      ],
    }),
  },
  loop: {
    label: "Loop",
    group: "logic",
    icon: RepeatIcon,
    summary: "Runs its body once for each item in a list.",
    keywords: "for each iterate repeat list items",
    idPrefix: "loop",
    defaults: () => ({
      items: "",
      item_name: "item",
      max_iterations: 100,
      concurrency: 1,
    }),
  },
  merge: {
    label: "Merge",
    group: "logic",
    icon: GitMergeIcon,
    summary: "Waits for the ways coming into it, then goes on as one.",
    keywords: "join wait parallel combine",
    idPrefix: "merge",
    defaults: () => ({ mode: "all" }),
  },
  end: {
    label: "End",
    group: "finish",
    icon: Flag02Icon,
    summary: "Finishes the run, as succeeded or failed, with a result.",
    keywords: "finish stop return result output fail",
    terminal: true,
    idPrefix: "end",
    defaults: () => ({ outcome: "succeeded", result: "" }),
  },
}

export const STEP_KIND_LIST = Object.keys(STEP_KINDS) as StepKind[]

export const isStepKind = (value: unknown): value is StepKind =>
  typeof value === "string" && Object.hasOwn(STEP_KINDS, value)

export function kindInfo<K extends StepKind>(kind: K): KindInfo<K> {
  return STEP_KINDS[kind] as KindInfo<K>
}

/** A new step of a kind, with its defaults. */
export function newStep<K extends StepKind>(kind: K, name?: string): StepData<K> {
  const info = kindInfo(kind)
  return { kind, name: name ?? info.label, config: info.defaults() } as StepData<K>
}

/** The glyph a step shows: an entry point's says how it starts. */
export function stepIcon(step: StepData): IconProp {
  return STEP_KINDS[step.kind].icon
}

export const TRIGGERS: Record<
  TriggerKind,
  { label: string; icon: IconProp; description: string }
> = {
  manual: {
    label: "By hand",
    icon: CursorPointer01Icon,
    description: "A person starts it, with the input they give.",
  },
  event: {
    label: "On an event",
    icon: WebhookIcon,
    description: "An event of any of the event types it's linked to starts it.",
  },
  schedule: {
    label: "On a schedule",
    icon: Calendar03Icon,
    description: "It starts on a cron schedule.",
  },
}

export const HTTP_METHODS: HttpMethod[] = ["GET", "POST", "PUT", "PATCH", "DELETE"]

/**
 * How long an agent's model thinks before it answers, from least to most:
 * the levels model_provider.yaml knows. A model runs at the level asked for
 * if it offers it, else the nearest above, else the nearest below.
 */
export const THINKING_LEVELS = ["off", "minimal", "low", "medium", "high", "xhigh"] as const
export type ThinkingLevel = (typeof THINKING_LEVELS)[number]

export const DELAY_UNITS: { value: DelayUnit; label: string }[] = [
  { value: "seconds", label: "Seconds" },
  { value: "minutes", label: "Minutes" },
  { value: "hours", label: "Hours" },
  { value: "days", label: "Days" },
]

/* -------------------------------------------------------------------------- */
/* IDs                                                                        */
/* -------------------------------------------------------------------------- */

/** A step's ID: what connections and expressions (`steps.<id>`) call it. */
export const STEP_ID_PATTERN = /^[a-z][a-z0-9_]{0,47}$/

const ID_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"

/**
 * A new step's ID, made by the builder, never by a person: its kind and a
 * random suffix (`agent_k3f9x`), unlike any ID the workflow has, so a step
 * that's deleted and made again never inherits expressions that named the
 * old one. The start step is `start`.
 */
export function nextStepId(kind: StepKind, taken: Iterable<string>) {
  const used = new Set(taken)
  if (kind === "entry" && !used.has("start")) return "start"
  const prefix = STEP_KINDS[kind].idPrefix
  for (;;) {
    const random = crypto.getRandomValues(new Uint8Array(5))
    const id = `${prefix}_${Array.from(random, (b) => ID_ALPHABET[b % ID_ALPHABET.length]).join("")}`
    if (!used.has(id)) return id
  }
}

/** What a step's name would make as an ID: "Read issue" → `read_issue`. */
export function slugify(name: string) {
  const slug = name
    .toLowerCase()
    .normalize("NFKD")
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "")
    .replace(/^[^a-z]+/, "")
    .slice(0, 48)
  return slug
}
