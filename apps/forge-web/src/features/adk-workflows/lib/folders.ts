import type { AgentKind, AgentStep } from "./model"

/*
 * Where the nodes that work in the run's folder work: a clone, the git
 * steps after it, a Bash step, a Pi agent. Each has a folder setting, a
 * template naming a folder of the run's folder (relative to it, starting
 * with {{ state.workspace }}, or another step's folder), and hands the
 * folder on. Worked out here as far as the builder can tell before a run,
 * so a field can say where it'll be, and offer the folders earlier steps
 * work in.
 */

/** The nodes that work in a folder: their folder setting, and what they hand it on as. */
export const FOLDER_SETTINGS: Partial<
  Record<AgentKind, { setting: string; output: string }>
> = {
  git_clone: { setting: "folder", output: "folder" },
  git_branch: { setting: "folder", output: "folder" },
  git_push: { setting: "folder", output: "folder" },
  bash: { setting: "cwd", output: "cwd" },
  pi_agent: { setting: "folder", output: "folder" },
}

/** One part of a folder's path from the run's folder. */
export type FolderPart = {
  text: string
  /** Filled in as the run goes: a {{ }} template, or a name not known yet. */
  later?: boolean
}

/** Where a node works, as far as the builder can tell. */
export type FolderPlace = {
  /** Its path from the run's folder, part by part; none for the run's folder. */
  parts: FolderPart[]
  /** The step whose folder it is (or is in), when it's given as one's. */
  from?: { id: string; name: string }
  /** It leads outside the run's folder: the step would fail. */
  outside?: boolean
}

// A step's folder, given whole: {{ steps.<id>.output.folder }} (or .cwd).
const STEP_FOLDER =
  /^\{\{\s*steps\.([A-Za-z_][A-Za-z0-9_]*)\.output\.(folder|cwd)\s*\}\}/
// The run's folder, given whole.
const WORKSPACE = /^\{\{\s*state\.workspace\s*\}\}/

/** A path's parts, split at the slashes outside its {{ }}. */
function split(path: string): string[] {
  const parts: string[] = []
  let part = ""
  let depth = 0
  for (let i = 0; i < path.length; i += 1) {
    if (path.startsWith("{{", i)) depth += 1
    if (path.startsWith("}}", i) && depth) depth -= 1
    if (path[i] === "/" && !depth) {
      parts.push(part)
      part = ""
    } else part += path[i]
  }
  parts.push(part)
  return parts
}

/** A repository's name, as a clone names its folder after it; none when it's a template. */
export function repositoryName(repository: string): string | undefined {
  const written = repository.trim()
  if (!written || written.includes("{{")) return undefined
  const last = written.replace(/\/+$/, "").split("/").pop() ?? ""
  return last.replace(/\.git$/, "") || undefined
}

/** The folder a node of a kind that works in one works in when its setting is empty. */
function emptyPlace(step: AgentStep): FolderPart[] {
  if (step.kind !== "git_clone") return []
  const name = repositoryName(step.config.repository)
  return [
    name ? { text: name } : { text: "the repository's name", later: true },
  ]
}

/** A folder setting's value, as the node it's on would read it. */
export function placeOf(
  value: string,
  step: AgentStep | undefined,
  steps: Map<string, AgentStep>,
  seen: Set<string> = new Set()
): FolderPlace {
  let rest = value.trim()
  if (!rest) return { parts: step ? emptyPlace(step) : [] }
  let parts: FolderPart[] = []
  let from: FolderPlace["from"]
  const given = STEP_FOLDER.exec(rest)
  const workspace = WORKSPACE.exec(rest)
  if (given) {
    const other = steps.get(given[1])
    if (other && !seen.has(given[1]) && FOLDER_SETTINGS[other.kind]) {
      const place = folderOf(given[1], steps, new Set([...seen, given[1]]))
      parts = place.parts
      from = { id: given[1], name: other.name }
      if (place.outside) return { parts, from, outside: true }
    } else parts = [{ text: given[0], later: true }]
    rest = rest.slice(given[0].length)
  } else if (workspace) {
    rest = rest.slice(workspace[0].length)
  } else if (rest.startsWith("/") || rest.startsWith("~")) {
    // Only a path inside the run's folder may be absolute: not one written out.
    return { parts: [{ text: rest }], outside: true }
  }
  for (const part of split(rest)) {
    if (!part.trim() || part === ".") continue
    if (part === "..") {
      if (!parts.length) return { parts, from, outside: true }
      parts = parts.slice(0, -1)
    } else parts = [...parts, { text: part, later: part.includes("{{") }]
  }
  return { parts, from }
}

/** Where a node that works in a folder works; the run's folder for any other. */
export function folderOf(
  id: string,
  steps: Map<string, AgentStep>,
  seen: Set<string> = new Set([id])
): FolderPlace {
  const step = steps.get(id)
  const folder = step && FOLDER_SETTINGS[step.kind]
  if (!step || !folder) return { parts: [] }
  const config = step.config as unknown as Record<string, unknown>
  const value = config[folder.setting]
  return placeOf(typeof value === "string" ? value : "", step, steps, seen)
}

/** A place, as a line of text: `backend/app`, or the run's folder. */
export const placeLabel = (place: FolderPlace) =>
  place.parts.length
    ? place.parts.map((part) => part.text).join("/")
    : "the run's folder"

/** A folder an earlier step works in, as a folder field offers it. */
export type FolderChoice = {
  id: string
  name: string
  kind: AgentKind
  /** What the field is set to, to work there: `{{ steps.<id>.output.folder }}`. */
  value: string
  place: FolderPlace
}

/**
 * The folders the steps in `earlier` work in (a clone's, a Pi agent's...):
 * what a later node's folder can be set to.
 */
export function folderChoices(
  earlier: Iterable<string>,
  steps: Map<string, AgentStep>
): FolderChoice[] {
  const choices: FolderChoice[] = []
  for (const id of earlier) {
    const step = steps.get(id)
    const folder = step && FOLDER_SETTINGS[step.kind]
    if (!step || !folder) continue
    choices.push({
      id,
      name: step.name,
      kind: step.kind,
      value: `{{ steps.${id}.output.${folder.output} }}`,
      place: folderOf(id, steps),
    })
  }
  return choices
}
