/**
 * How the file viewer shows a file, from its name (and, for names it
 * doesn't know, its media type). Every renderer runs in the browser on the
 * file's own bytes; formats no browser library reads well (legacy binary
 * Office, OpenDocument text and slides, RTF, Visio) have no preview and say
 * why, with the format's name for the message.
 */
export type PreviewKind =
  | "pdf"
  | "word"
  | "slides"
  | "sheet"
  | "markdown"
  | "text"
  | "mermaid"
  | "image"
  | "none"

const BY_EXTENSION: Record<string, PreviewKind> = {
  pdf: "pdf",
  docx: "word",
  docm: "word",
  dotx: "word",
  dotm: "word",
  pptx: "slides",
  pptm: "slides",
  ppsx: "slides",
  ppsm: "slides",
  potx: "slides",
  potm: "slides",
  // SheetJS reads the legacy and OpenDocument workbooks too.
  xlsx: "sheet",
  xlsm: "sheet",
  xlsb: "sheet",
  xls: "sheet",
  xlt: "sheet",
  xltx: "sheet",
  ods: "sheet",
  csv: "sheet",
  tsv: "sheet",
  md: "markdown",
  markdown: "markdown",
  mdx: "markdown",
  mmd: "mermaid",
  mermaid: "mermaid",
  png: "image",
  jpg: "image",
  jpeg: "image",
  gif: "image",
  webp: "image",
  avif: "image",
  bmp: "image",
  ico: "image",
  // Shown with <img>, where its scripts never run.
  svg: "image",
}

const TEXT_EXTENSIONS = new Set([
  "txt",
  "text",
  "log",
  "json",
  "jsonl",
  "ndjson",
  "yaml",
  "yml",
  "toml",
  "ini",
  "cfg",
  "conf",
  "env",
  "xml",
  "html",
  "htm",
  "css",
  "js",
  "mjs",
  "cjs",
  "ts",
  "tsx",
  "jsx",
  "py",
  "go",
  "java",
  "kt",
  "rb",
  "rs",
  "c",
  "h",
  "cpp",
  "cs",
  "php",
  "sh",
  "bash",
  "zsh",
  "sql",
  "graphql",
  "proto",
  "diff",
  "patch",
  "rst",
  "adoc",
  "tex",
])

/**
 * Formats Forge indexes but a browser can't show: what they are, and the
 * app that opens them.
 */
const NO_PREVIEW: Record<string, { format: string; app: string }> = {
  doc: { format: "Word 97–2003", app: "Word" },
  dot: { format: "Word 97–2003 template", app: "Word" },
  rtf: { format: "Rich Text", app: "a word processor" },
  odt: { format: "OpenDocument text", app: "LibreOffice or Word" },
  ppt: { format: "PowerPoint 97–2003", app: "PowerPoint" },
  pps: { format: "PowerPoint 97–2003 show", app: "PowerPoint" },
  pot: { format: "PowerPoint 97–2003 template", app: "PowerPoint" },
  odp: {
    format: "OpenDocument presentation",
    app: "LibreOffice or PowerPoint",
  },
  vsd: { format: "Visio 2003–2010", app: "Visio" },
  vsdx: { format: "Visio", app: "Visio" },
  vsdm: { format: "Visio", app: "Visio" },
}

export const extensionOf = (name: string) => {
  const dot = name.lastIndexOf(".")
  return dot > 0 ? name.slice(dot + 1).toLowerCase() : ""
}

export function previewKindOf(name: string, mediaType = ""): PreviewKind {
  const extension = extensionOf(name)
  const known = BY_EXTENSION[extension]
  if (known) return known
  if (TEXT_EXTENSIONS.has(extension)) return "text"
  if (NO_PREVIEW[extension]) return "none"
  const type = mediaType.split(";")[0].trim().toLowerCase()
  if (type === "application/pdf") return "pdf"
  if (type.startsWith("image/")) return "image"
  if (type.startsWith("text/") || /[+/]json$|[+/]xml$/.test(type)) return "text"
  return "none"
}

/** Whether the viewer can show a file, e.g. to offer to open it. */
export const canPreview = (name: string, mediaType?: string) =>
  previewKindOf(name, mediaType) !== "none"

/** What a file without a preview is, for the message: "Visio", ".bin". */
export function formatName(name: string) {
  const extension = extensionOf(name)
  return (
    NO_PREVIEW[extension]?.format ??
    (extension ? `.${extension}` : "this kind of")
  )
}

/** The app that opens a file without a preview, when there's a usual one. */
export const noPreviewApp = (name: string) => NO_PREVIEW[extensionOf(name)]?.app
