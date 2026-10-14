/*
 * The steps a workflow takes beside its agents: a person approving,
 * actions (an HTTP request, a Bash command, a transform, a filter, a
 * delay), logic (if, switch, match, loop, for each, merge) and its ends.
 * Each has its kind's settings (`config`). What each kind is called, its
 * ways out and what a new one starts with are the admin API's node
 * catalog's (adk-workflows/lib/catalog.ts), which adk-workflows/lib builds
 * its node kinds on.
 */

/* -------------------------------------------------------------------------- */
/* Settings, per kind                                                         */
/* -------------------------------------------------------------------------- */

export type HttpMethod = "GET" | "POST" | "PUT" | "PATCH" | "DELETE"
export type DelayUnit = "seconds" | "minutes" | "hours" | "days"

/** A header, or a query parameter, of an HTTP request: its name and its value. */
export type HeaderRow = { id: string; name: string; value: string }
/**
 * A way out of an HTTP request of its own, for the statuses it names
 * (`codes`: `404`, `401, 403`, `4xx`, `500-599`): taken instead of Success
 * or Error, handing on the response.
 */
export type StatusWay = { id: string; codes: string }
/** A case of a switch: the value that sends the run its way. */
export type SwitchCase = { id: string; value: string }
/** A rule of a match: its label and the condition that takes it. */
export type MatchArm = { id: string; label: string; condition: string }
/** Whether a match takes the first rule that holds, or every one at once. */
export type MatchMode = "first" | "all"
/** A loop's or a for-each's: the list it goes through, and how. */
export type EachConfig = {
  /** An expression giving the list to go through. */
  items: string
  /** What the body calls the current item. */
  item_name: string
  max_iterations: number
  /** How many items run at once; 1 runs them in order. */
  concurrency: number
}

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
    /**
     * Query parameters, added to the URL's own: each name and value a
     * template, filled in and then percent-encoded whole as it's sent.
     */
    query: HeaderRow[]
    headers: HeaderRow[]
    /** A JSONata expression making the body: an object or list goes as JSON. */
    body: string
    timeout_seconds: number
    retries: number
    /** A JSON Schema of the response's body; `{}` leaves it undeclared. */
    output_schema: Record<string, unknown>
    /**
     * One of the organization's credentials, by ID (the Credentials page):
     * how it signs its requests, sent only to the URLs it's for. Empty for none.
     */
    credential: string
    /**
     * Ways out of their own, before Success and Error: the first whose codes
     * hold the response's status is taken (not retried). One nothing leads
     * on from ends the run's way there, quietly.
     */
    statuses: StatusWay[]
  }
  bash: {
    /**
     * bash, run as it's written: never filled in with {{ }}, so the run's
     * data can't become code. It reads its environment variables instead.
     */
    command: string
    /**
     * The folder it runs in: a template, a folder of the run's folder (made
     * when it isn't there), or the run's folder itself when empty.
     */
    cwd: string
    /** Environment variables: each name, and its value, a template. */
    env: HeaderRow[]
    timeout_seconds: number
  }
  /** Clones a repository into a folder of the run's folder. */
  git_clone: {
    /** A template: owner/name on GitHub, or an https:// URL. */
    repository: string
    /** A template: the branch or tag checked out; empty for its default branch. */
    ref: string
    /** A template: a folder of the run's folder; empty for one named after the repository. */
    folder: string
    /** How many of the latest commits it gets; 0 for the whole history. */
    depth: number
    /** One of the organization's credentials, by ID: signs git in. Empty for none. */
    credential: string
    timeout_seconds: number
  }
  /** Makes a branch in a cloned repository and checks it out. */
  git_branch: {
    /** A template: the repository's folder in the run's folder; empty for the run's folder. */
    folder: string
    /** A template: the branch's name. */
    branch: string
    /** A template: where it starts; empty for what's checked out. */
    start: string
    timeout_seconds: number
  }
  /** Commits what changed in a cloned repository and pushes its branch. */
  git_push: {
    folder: string
    /** A template: the commit's message. */
    message: string
    /** Templates: who commits; empty for Forge. */
    author_name: string
    author_email: string
    credential: string
    timeout_seconds: number
  }
  /** Opens a pull request on GitHub. */
  github_pull_request: {
    /** A template: owner/name, or the repository's https:// URL. */
    repository: string
    /** A template: the branch it merges (owner:branch from a fork). */
    head: string
    /** A template: the branch it merges into; empty for the default branch. */
    base: string
    title: string
    body: string
    draft: boolean
    credential: string
    timeout_seconds: number
  }
  transform: {
    /** A JSONata expression: what it hands on. */
    expression: string
    /** A JSON Schema of what it makes; `{}` leaves it undeclared. */
    output_schema: Record<string, unknown>
  }
  filter: {
    /** An expression giving the list to filter. */
    items: string
    /** What the condition calls the item it's on. */
    item_name: string
    /** An expression read per item: the items it's true for are kept. */
    condition: string
  }
  delay: { amount: number; unit: DelayUnit }
  if: { condition: string }
  switch: { value: string; cases: SwitchCase[] }
  match: { arms: MatchArm[]; mode: MatchMode }
  /** Its body comes back to it with each item's result. */
  loop: EachConfig
  /** Its body never comes back: each item's way ends where it ends. */
  for_each: EachConfig
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

export { INPUT, type StepOutput } from "@/features/builder/lib/types"

/* -------------------------------------------------------------------------- */
/* IDs                                                                        */
/* -------------------------------------------------------------------------- */

let uidCounter = 0
/** A short ID for a case, rule, route or header. Unique within the page. */
export function uid(prefix: string) {
  uidCounter += 1
  return `${prefix}_${Date.now().toString(36).slice(-4)}${uidCounter.toString(36)}`
}

/**
 * The steps that run a body per item: a loop (its body comes back to it)
 * and a for-each (its body doesn't).
 */
export const BODY_KINDS: ReadonlySet<string> = new Set(["loop", "for_each"])

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
