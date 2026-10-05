import {
  Flag02Icon,
  FunctionIcon,
  GitForkIcon,
  GitMergeIcon,
  Globe02Icon,
  HourglassIcon,
  LeftToRightListBulletIcon,
  RepeatIcon,
  Route02Icon,
  UserCheck01Icon,
} from "@hugeicons/core-free-icons"

import type { IconProp } from "@/components/forge/icons"
import type { StepOutput } from "@/features/builder/lib/types"

/*
 * The steps a workflow takes beside its agents: a person approving,
 * actions (an HTTP request, a transform, a delay), logic (if, switch,
 * match, loop, merge) and its ends. Each is a kind from the catalog below,
 * with that kind's settings (`config`) and the outputs it can leave by:
 * one `next` for most, a named output per branch for the logic steps.
 * adk-workflows/lib builds its node kinds on these.
 */

/* -------------------------------------------------------------------------- */
/* Settings, per kind                                                         */
/* -------------------------------------------------------------------------- */

export type HttpMethod = "GET" | "POST" | "PUT" | "PATCH" | "DELETE"
export type DelayUnit = "seconds" | "minutes" | "hours" | "days"

/** A header of an HTTP request. */
export type HeaderRow = { id: string; name: string; value: string }
/** A case of a switch: the value that sends the run its way. */
export type SwitchCase = { id: string; value: string }
/** A rule of a match: its label and the condition that takes it. */
export type MatchArm = { id: string; label: string; condition: string }

export type StepConfigs = {
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

export { INPUT, type StepOutput } from "@/features/builder/lib/types"

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

type KindInfo<K extends StepKind> = {
  label: string
  icon: IconProp
  /** One line: what the step does. */
  summary: string
  /** Words the library's filter also matches. */
  keywords: string
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
  approval: {
    label: "Approval",
    icon: UserCheck01Icon,
    summary: "The run waits for a person to approve or reject.",
    keywords: "human review person approve reject handoff sign off",
    person: true,
    idPrefix: "approval",
    defaults: () => ({ message: "", approvers: "org:admin", timeout_hours: 0 }),
  },
  http: {
    label: "HTTP request",
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
    icon: FunctionIcon,
    summary: "Reshapes data with a JSONata expression.",
    keywords: "map reshape jsonata expression object data",
    idPrefix: "transform",
    defaults: () => ({ expression: "", output_schema: {} }),
  },
  delay: {
    label: "Delay",
    icon: HourglassIcon,
    summary: "Waits a while before going on.",
    keywords: "wait sleep pause timer",
    idPrefix: "delay",
    defaults: () => ({ amount: 5, unit: "minutes" }),
  },
  if: {
    label: "If / else",
    icon: GitForkIcon,
    summary: "Takes one of two ways, on a condition.",
    keywords: "condition branch boolean true false",
    idPrefix: "if",
    defaults: () => ({ condition: "" }),
  },
  switch: {
    label: "Switch",
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
    icon: GitMergeIcon,
    summary: "Waits for the ways coming into it, then goes on as one.",
    keywords: "join wait parallel combine",
    idPrefix: "merge",
    defaults: () => ({ mode: "all" }),
  },
  end: {
    label: "End",
    icon: Flag02Icon,
    summary: "Finishes the run, as succeeded or failed, with a result.",
    keywords: "finish stop return result output fail",
    terminal: true,
    idPrefix: "end",
    defaults: () => ({ outcome: "succeeded", result: "" }),
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
