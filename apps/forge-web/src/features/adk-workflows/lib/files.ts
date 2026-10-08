/*
 * The files a workflow's start can take: when it allows files
 * (`allow_files`), of the types it names (`file_types`; none takes any
 * type). A run is sent them with its input, and the admin API saves each
 * as an ADK artifact of the run's session, where its steps read them
 * (`ctx.load_artifact(name)`, an LLM agent's `load_artifacts` tool) and
 * `state.files` lists them.
 *
 * The admin API's list of the types (with each extension's media type) is
 * packages/python/adk-workflows/src/forge_task_adk_workflows/files.py:
 * keep the two alike (its tests compare that one with this format's schema).
 */

export type FileTypeId = keyof typeof FILE_TYPES

export type FileTypeGroup =
  "documents" | "data" | "images" | "media" | "archives"

export type FileTypeInfo = {
  label: string
  group: FileTypeGroup
  /** Its file name extensions, with their dots. */
  extensions: string[]
}

export const FILE_TYPES = {
  pdf: { label: "PDF", group: "documents", extensions: [".pdf"] },
  word: { label: "Word", group: "documents", extensions: [".docx", ".doc"] },
  powerpoint: {
    label: "PowerPoint",
    group: "documents",
    extensions: [".pptx", ".ppt"],
  },
  text: { label: "Plain text", group: "documents", extensions: [".txt"] },
  markdown: {
    label: "Markdown",
    group: "documents",
    extensions: [".md", ".markdown"],
  },
  html: { label: "HTML", group: "documents", extensions: [".html", ".htm"] },
  excel: { label: "Excel", group: "data", extensions: [".xlsx", ".xls"] },
  csv: { label: "CSV", group: "data", extensions: [".csv"] },
  json: { label: "JSON", group: "data", extensions: [".json"] },
  xml: { label: "XML", group: "data", extensions: [".xml"] },
  png: { label: "PNG", group: "images", extensions: [".png"] },
  jpeg: { label: "JPEG", group: "images", extensions: [".jpg", ".jpeg"] },
  gif: { label: "GIF", group: "images", extensions: [".gif"] },
  webp: { label: "WebP", group: "images", extensions: [".webp"] },
  mp3: { label: "MP3 audio", group: "media", extensions: [".mp3"] },
  wav: { label: "WAV audio", group: "media", extensions: [".wav"] },
  mp4: { label: "MP4 video", group: "media", extensions: [".mp4"] },
  zip: { label: "ZIP archive", group: "archives", extensions: [".zip"] },
} as const satisfies Record<string, FileTypeInfo>

export const FILE_TYPE_IDS = Object.keys(
  FILE_TYPES
) as (keyof typeof FILE_TYPES)[]

export const FILE_TYPE_GROUPS: { id: FileTypeGroup; label: string }[] = [
  { id: "documents", label: "Documents" },
  { id: "data", label: "Data" },
  { id: "images", label: "Images" },
  { id: "media", label: "Audio and video" },
  { id: "archives", label: "Archives" },
]

export const isFileType = (value: unknown): value is FileTypeId =>
  typeof value === "string" && value in FILE_TYPES

/** What files a start takes. */
export type FilesRule = {
  allowed: boolean
  /** None takes any type. */
  types: FileTypeId[]
}

export const NO_FILES: FilesRule = { allowed: false, types: [] }

/** The types, in the catalog's order. */
const ordered = (types: readonly FileTypeId[]) =>
  FILE_TYPE_IDS.filter((id) => types.includes(id))

/** `PDF, Word and Excel`, or `any type`. */
export function describeTypes(types: readonly FileTypeId[]): string {
  const labels = ordered(types).map((id) => FILE_TYPES[id].label)
  if (!labels.length) return "any type"
  if (labels.length === 1) return labels[0]
  return `${labels.slice(0, -1).join(", ")} and ${labels[labels.length - 1]}`
}

/** A file input's `accept`: the types' extensions; empty takes any. */
export const acceptOf = (types: readonly FileTypeId[]): string =>
  ordered(types)
    .flatMap((id) => FILE_TYPES[id].extensions)
    .join(",")

/** Whether a start takes a file by its name; any name when it names no types. */
export function takesFile(rule: FilesRule, name: string): boolean {
  if (!rule.allowed) return false
  if (!rule.types.length) return true
  const dot = name.lastIndexOf(".")
  const extension = dot > 0 ? name.slice(dot).toLowerCase() : ""
  return rule.types.some((id) =>
    (FILE_TYPES[id].extensions as readonly string[]).includes(extension)
  )
}
