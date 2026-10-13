import type { IssueLevel } from "@/features/builder/lib/types"
import { resolveExpression } from "./expressions"
import type { StepData } from "./model"
import type { Scope } from "./scope"
import { oneOf, valuesOf } from "./types"

/*
 * What's wrong in a Forge step's own settings, for a workflow's
 * checks (adk-workflows/lib/validate): errors (it can't run) and warnings (it
 * can, but probably not as meant). And the cycles in a graph, which both
 * builders look for.
 */

// A name a JSONata path can use without backticks.
const IDENTIFIER = /^[A-Za-z_][A-Za-z0-9_]*$/

/**
 * What every step reads by name: an item called one of these would hide
 * it from the steps that read the item.
 */
export const READ_NAMES: Record<string, string> = {
  input: "the run's input",
  previous: "what the step before handed on",
  steps: "the earlier steps",
  state: "the session's state",
  index: "the item's place in the list",
}

/** What's wrong with what a loop, a for-each or a filter calls its item. */
function itemNameIssue(name: string): string | undefined {
  if (!IDENTIFIER.test(name))
    return "The item's name is one word: letters, digits and _."
  const taken = READ_NAMES[name]
  return taken
    ? `${name} is ${taken}, which the steps reading the item read too: call the item something else.`
    : undefined
}
// A name bash reads as a variable.
const VARIABLE = /^[A-Za-z_][A-Za-z0-9_]*$/
/** The variable every Bash step's command reads its run's folder from. */
export const WORKSPACE_VARIABLE = "WORKSPACE"
// The run's data written into a command as if it were filled in: it isn't.
const FILLED_IN = /\{\{\s*(input|previous|steps|state|index)\b/

/** What's wrong with a Bash step's environment variables. */
function variableIssue(names: string[]): string | undefined {
  if (names.some((name) => !name)) return "Every variable needs a name."
  const odd = names.find((name) => !VARIABLE.test(name))
  if (odd)
    return `${odd} isn't a name bash reads: letters, digits and _, not starting with a digit.`
  if (names.includes(WORKSPACE_VARIABLE))
    return `${WORKSPACE_VARIABLE} is the run's folder; call the variable something else.`
  const twice = names.find((name, i) => names.indexOf(name) !== i)
  return twice ? `Two variables are called ${twice}.` : undefined
}

/**
 * Whether a folder written out (no {{ }} in it) leads outside the run's
 * folder: an absolute path (the run's folder isn't known until it runs),
 * or one whose `..` go above it.
 */
export function leavesRunFolder(path: string): boolean {
  const written = path.trim()
  if (!written || written.includes("{{")) return false
  if (written.startsWith("/") || written.startsWith("~")) return true
  let depth = 0
  for (const part of written.split("/")) {
    if (part === "..") depth -= 1
    else if (part && part !== ".") depth += 1
    if (depth < 0) return true
  }
  return false
}

// A repository as the GitHub steps take one: owner/name (on GitHub), or an
// https:// URL.
const REPOSITORY =
  /^(?:(?:github\.com\/)?[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+|https:\/\/\S+)$/

/** What's wrong with a repository written out (no {{ }} in it). */
function repositoryIssue(value: string): string | undefined {
  const written = value.trim()
  if (!written) return "Give the repository: owner/name on GitHub, or its https:// URL."
  if (written.includes("{{") || REPOSITORY.test(written)) return undefined
  return written.startsWith("git@") || written.startsWith("ssh://")
    ? "Give its https:// URL: the GitHub steps sign in over HTTPS, not SSH."
    : "Give owner/name on GitHub, or the repository's https:// URL."
}

// What git won't take in a branch's name, written out (no {{ }} in it).
const NOT_A_BRANCH = /\s|\.\.|[~^:?*[\\]|^[-/]|\/$|\.lock$|@\{|^@$/

/** What's wrong with a branch's name written out. */
function branchIssue(value: string, what: string): string | undefined {
  const written = value.trim()
  if (!written) return `Give ${what}.`
  if (written.includes("{{") || !NOT_A_BRANCH.test(written)) return undefined
  return `${written} isn't a name git takes for a branch: no spaces, .., ~ ^ : ? * [ or \\, and not starting with - or /.`
}

// An address: http(s) and no spaces except inside its {{ }} parts, or an
// expression in {{ }}.
const URLISH =
  /^(https?:\/\/(?:\{\{[^}]*\}\}|[^\s{]|\{(?!\{))+|\{\{[\s\S]*\}\}\S*)$/

/** What's missing or wrong in one step's own settings. */
export function settingsIssues(
  step: StepData
): [code: string, level: IssueLevel, message: string][] {
  const out: [string, IssueLevel, string][] = []
  const need = (ok: boolean, code: string, message: string) => {
    if (!ok) out.push([code, "error", message])
  }
  const warn = (ok: boolean, code: string, message: string) => {
    if (!ok) out.push([code, "warning", message])
  }
  switch (step.kind) {
    case "approval":
      warn(
        Boolean(step.config.message.trim()),
        "message",
        "Tell the approver what they're deciding."
      )
      break
    case "http": {
      const c = step.config
      if (!c.url.trim()) need(false, "url", "Give it a URL.")
      else {
        need(
          URLISH.test(c.url.trim()),
          "url",
          "A URL starts with https:// or is an expression in {{ }}."
        )
      }
      need(
        c.query.every((q) => q.name.trim()),
        "query",
        "Every query parameter needs a name."
      )
      need(
        c.headers.every((h) => h.name.trim()),
        "headers",
        "Every header needs a name."
      )
      warn(
        !(c.method === "GET" && c.body.trim()),
        "body",
        "A GET request sends no body; pick another method or clear it."
      )
      break
    }
    case "bash": {
      const c = step.config
      need(Boolean(c.command.trim()), "command", "Write the command it runs.")
      const found = c.command.match(FILLED_IN)
      warn(
        !found,
        "command-filled",
        `The command runs as it's written: ${found?.[0] ?? "{{"} … }} isn't filled in. Give it a variable with that value, and read it as "$NAME".`
      )
      need(
        !leavesRunFolder(c.cwd),
        "cwd",
        "Its folder is in the run's folder: give a path from it (backend), or start it with {{ state.workspace }}."
      )
      const variables = variableIssue(c.env.map((v) => v.name.trim()))
      need(!variables, "env", variables ?? "")
      need(c.timeout_seconds >= 1, "timeout", "Give it at least a second.")
      break
    }
    case "git_clone": {
      const c = step.config
      const repository = repositoryIssue(c.repository)
      need(!repository, "repository", repository ?? "")
      need(
        !leavesRunFolder(c.folder),
        "folder",
        "Its folder is in the run's folder: give a path from it (backend), or leave it empty for one named after the repository."
      )
      need(
        Number.isInteger(c.depth) && c.depth >= 0,
        "depth",
        "Give how many of the latest commits it gets, or 0 for all of them."
      )
      need(c.timeout_seconds >= 1, "timeout", "Give it at least a second.")
      break
    }
    case "git_branch": {
      const c = step.config
      const branch = branchIssue(c.branch, "the branch's name")
      need(!branch, "branch", branch ?? "")
      need(
        !leavesRunFolder(c.folder),
        "folder",
        "Its folder is in the run's folder: give the clone's folder, such as {{ steps.clone.output.folder }}."
      )
      need(c.timeout_seconds >= 1, "timeout", "Give it at least a second.")
      break
    }
    case "git_push": {
      const c = step.config
      need(
        Boolean(c.message.trim()),
        "message",
        "Write the commit's message."
      )
      need(
        !leavesRunFolder(c.folder),
        "folder",
        "Its folder is in the run's folder: give the clone's folder, such as {{ steps.clone.output.folder }}."
      )
      warn(
        !c.author_email.trim() ||
          c.author_email.includes("{{") ||
          c.author_email.includes("@"),
        "author-email",
        "That isn't an email address."
      )
      warn(
        Boolean(c.credential),
        "credential",
        "Without a credential, GitHub refuses the push: pick one with a token for https://github.com."
      )
      need(c.timeout_seconds >= 1, "timeout", "Give it at least a second.")
      break
    }
    case "github_pull_request": {
      const c = step.config
      const repository = repositoryIssue(c.repository)
      need(!repository, "repository", repository ?? "")
      const head = branchIssue(c.head.replace(/^[^:\s]+:/, ""), "the branch it merges")
      need(!head, "head", head ?? "")
      if (c.base.trim()) {
        const base = branchIssue(c.base, "the branch it merges into")
        need(!base, "base", base ?? "")
      }
      need(Boolean(c.title.trim()), "title", "Give the pull request a title.")
      need(
        Boolean(c.credential),
        "credential",
        "GitHub opens pull requests only for someone signed in: pick a credential with a token for https://api.github.com."
      )
      need(c.timeout_seconds >= 1, "timeout", "Give it at least a second.")
      break
    }
    case "transform":
      need(
        Boolean(step.config.expression.trim()),
        "expression",
        "Write the JSONata expression that makes what it hands on."
      )
      break
    case "filter":
      need(
        Boolean(step.config.items.trim()),
        "items",
        "Say which list it filters."
      )
      need(
        !itemNameIssue(step.config.item_name),
        "item-name",
        itemNameIssue(step.config.item_name) ?? ""
      )
      need(
        Boolean(step.config.condition.trim()),
        "condition",
        "Write the condition an item is kept by."
      )
      break
    case "delay":
      need(step.config.amount > 0, "amount", "Wait longer than zero.")
      break
    case "if":
      need(
        Boolean(step.config.condition.trim()),
        "condition",
        "Write the condition."
      )
      break
    case "switch": {
      const c = step.config
      need(Boolean(c.value.trim()), "value", "Say which value it switches on.")
      need(c.cases.length > 0, "cases", "Add a case.")
      need(
        c.cases.every((x) => x.value.trim()),
        "case-empty",
        "Every case needs the value that takes it."
      )
      const values = c.cases.map((x) => x.value.trim()).filter(Boolean)
      need(
        new Set(values).size === values.length,
        "case-twice",
        "Two cases have the same value; only the first would be taken."
      )
      break
    }
    case "match":
      need(step.config.arms.length > 0, "arms", "Add a rule.")
      need(
        step.config.arms.every((arm) => arm.condition.trim()),
        "arm-empty",
        "Every rule needs its condition."
      )
      break
    case "loop":
    case "for_each":
      need(
        Boolean(step.config.items.trim()),
        "items",
        "Say which list it goes through."
      )
      need(
        !itemNameIssue(step.config.item_name),
        "item-name",
        itemNameIssue(step.config.item_name) ?? ""
      )
      need(step.config.max_iterations >= 1, "max", "Allow at least one pass.")
      need(
        step.config.concurrency >= 1,
        "concurrency",
        "Run at least one item at a time."
      )
      break
    default:
      break
  }
  return out
}

/**
 * A Switch's cases against what it switches on: when that can only be
 * some values (a field declared with them, a boolean), a case for any
 * other could never be taken.
 */
export function switchIssues(
  step: StepData,
  scope: Scope
): [code: string, level: IssueLevel, message: string][] {
  if (step.kind !== "switch" || !step.config.value.trim()) return []
  const values = valuesOf(resolveExpression(step.config.value, scope))
  if (!values) return []
  return step.config.cases.flatMap((c): [string, IssueLevel, string][] =>
    !c.value.trim() || values.includes(c.value)
      ? []
      : [
          [
            `case-value-${c.id}`,
            "error",
            `The case ${JSON.stringify(c.value)} could never be taken: ${step.config.value.trim()} is ${oneOf(values)}.`,
          ],
        ]
  )
}

// The setting each of a step's own issues is about, by its code.
const FIELDS: Record<string, string> = {
  message: "message",
  url: "url",
  query: "query",
  headers: "headers",
  body: "body",
  command: "command",
  repository: "repository",
  folder: "folder",
  "folder-repository": "folder",
  depth: "depth",
  branch: "branch",
  "author-email": "author_email",
  credential: "credential",
  head: "head",
  base: "base",
  title: "title",
  "command-filled": "command",
  cwd: "cwd",
  env: "env",
  timeout: "timeout_seconds",
  expression: "expression",
  amount: "amount",
  condition: "condition",
  value: "value",
  cases: "cases",
  "case-empty": "cases",
  "case-twice": "cases",
  arms: "arms",
  "arm-empty": "arms",
  items: "items",
  "item-name": "item_name",
  max: "max_iterations",
  concurrency: "concurrency",
}

/**
 * The setting an issue of a step is about, by the issue's code: an
 * expression's (`field:<key>:<n>`), a case's, or one of the step's own.
 * None for the step as a whole (a way that goes nowhere).
 */
export function fieldOfCode(code: string): string | undefined {
  if (code.startsWith("field:")) return code.slice(6, code.lastIndexOf(":"))
  if (code.startsWith("case-value-")) return `cases.${code.slice(11)}`
  return FIELDS[code]
}

/** Tarjan's strongly connected components. */
export function stronglyConnected(
  ids: string[],
  outgoing: Map<string, { target: string }[]>
): string[][] {
  let index = 0
  const indices = new Map<string, number>()
  const low = new Map<string, number>()
  const onStack = new Set<string>()
  const stack: string[] = []
  const components: string[][] = []

  const visit = (id: string) => {
    indices.set(id, index)
    low.set(id, index)
    index += 1
    stack.push(id)
    onStack.add(id)
    for (const { target } of outgoing.get(id) ?? []) {
      if (!indices.has(target)) {
        visit(target)
        low.set(id, Math.min(low.get(id)!, low.get(target)!))
      } else if (onStack.has(target)) {
        low.set(id, Math.min(low.get(id)!, indices.get(target)!))
      }
    }
    if (low.get(id) === indices.get(id)) {
      const component: string[] = []
      let member: string
      do {
        member = stack.pop()!
        onStack.delete(member)
        component.push(member)
      } while (member !== id)
      components.push(component.reverse())
    }
  }
  for (const id of ids) if (!indices.has(id)) visit(id)
  return components
}
