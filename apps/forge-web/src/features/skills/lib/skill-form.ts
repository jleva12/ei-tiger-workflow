import { MagicWand01Icon } from "@hugeicons/core-free-icons"

import type { SkillBundle, SkillFile, SkillSource } from "./api"

/*
 * A skill as its editor holds it, and what's wrong with it: the checks the
 * admin API makes (forge_agent_runtime.skills.SkillBundle), so a skill
 * that's saved is one it takes.
 */

export const SKILLS_ICON = MagicWand01Icon

/** A skill's name: lowercase words joined by hyphens. */
export const NAME_PATTERN = /^[a-z0-9]+(-[a-z0-9]+)*$/
export const MAX_NAME = 64
export const MAX_DESCRIPTION = 1024
/** The most one of its files may be, in bytes. */
export const MAX_FILE_BYTES = 1_000_000
/** The most a skill may be: its instructions and files, in bytes. */
export const MAX_SKILL_BYTES = 4_000_000

/** A name made of any text: "PDF Forms" → "pdf-forms". */
export function slugOf(text: string) {
  return text
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .replace(/-{2,}/g, "-")
    .slice(0, MAX_NAME)
    .replace(/-+$/, "")
}

/** A file's size in bytes. */
export function fileBytes(file: SkillFile) {
  if (file.encoding === "base64")
    return Math.floor((file.content.replace(/=+$/, "").length * 3) / 4)
  return new TextEncoder().encode(file.content).length
}

export function skillBytes(skill: SkillBundle) {
  return (
    new TextEncoder().encode(skill.instructions).length +
    skill.files.reduce((sum, file) => sum + fileBytes(file), 0)
  )
}

export function formatBytes(bytes: number) {
  if (bytes < 1_000) return `${bytes} B`
  if (bytes < 1_000_000)
    return `${(bytes / 1_000).toFixed(bytes < 10_000 ? 1 : 0)} KB`
  return `${(bytes / 1_000_000).toFixed(1)} MB`
}

/** Why a file's path isn't one in the skill's folder; undefined when it is. */
export function pathProblem(path: string): string | undefined {
  const text = path.trim()
  if (!text) return "Give it a path, e.g. references/fields.md"
  if (text.startsWith("/") || /^[A-Za-z]:/.test(text))
    return "A path inside the skill's folder, without a leading /"
  if (
    text.split("/").some((part) => part === "" || part === "." || part === "..")
  )
    return "No empty, . or .. parts"
  if (text.toLowerCase() === "skill.md")
    return "SKILL.md is the name, description and instructions above"
  return undefined
}

export type SkillProblems = Partial<
  Record<"name" | "description" | "instructions" | "files", string>
> & { paths?: Record<number, string> }

/** What's wrong with a skill before it's saved; empty when nothing is. */
export function skillProblems(skill: SkillBundle): SkillProblems {
  const problems: SkillProblems = {}
  if (!skill.name) problems.name = "Name the skill"
  else if (skill.name.length > MAX_NAME)
    problems.name = `At most ${MAX_NAME} characters`
  else if (!NAME_PATTERN.test(skill.name))
    problems.name =
      "Lowercase letters and digits joined by single hyphens, e.g. pdf-forms"
  if (!skill.description.trim())
    problems.description = "Say what it does and when to use it"
  else if (skill.description.length > MAX_DESCRIPTION)
    problems.description = `At most ${MAX_DESCRIPTION} characters`
  if (!skill.instructions.trim())
    problems.instructions = "Write what the agent does when it uses the skill"
  const paths: Record<number, string> = {}
  const seen = new Set<string>()
  skill.files.forEach((file, index) => {
    const problem =
      pathProblem(file.path) ??
      (seen.has(file.path.trim()) ? "Another file has this path" : undefined) ??
      (fileBytes(file) > MAX_FILE_BYTES ? "Larger than 1 MB" : undefined)
    if (problem) paths[index] = problem
    seen.add(file.path.trim())
  })
  if (Object.keys(paths).length) problems.paths = paths
  if (skillBytes(skill) > MAX_SKILL_BYTES)
    problems.files =
      "The skill is larger than 4 MB, its files and instructions together"
  return problems
}

export const hasProblems = (problems: SkillProblems) =>
  Object.keys(problems).length > 0

/** A new skill's starting point. */
export function emptySkill(): SkillBundle {
  return {
    name: "",
    description: "",
    instructions:
      "# What this skill does\n\nOne line on the outcome.\n\n## Steps\n\n1. …\n2. …\n\n## What to hand back\n\n…\n",
    files: [],
  }
}

/** A file read from the person's disk: text when it's UTF-8, base64 otherwise. */
export async function skillFileOf(
  file: File,
  path: string
): Promise<SkillFile> {
  const bytes = new Uint8Array(await file.arrayBuffer())
  try {
    const text = new TextDecoder("utf-8", { fatal: true }).decode(bytes)
    return { path, content: text, encoding: "utf-8" }
  } catch {
    let binary = ""
    for (const byte of bytes) binary += String.fromCharCode(byte)
    return { path, content: btoa(binary), encoding: "base64" }
  }
}

/** Where a skill came from, in a few words. */
export function sourceLabel(source: SkillSource): string {
  switch (source.kind) {
    case "github": {
      const repo = source.repository.replace(/^https:\/\/github\.com\//, "")
      return `GitHub · ${repo}${source.path ? `/${source.path}` : ""}`
    }
    case "upload":
      return "Uploaded"
    case "creator":
      return "Skill creator"
    default:
      return "Written here"
  }
}
