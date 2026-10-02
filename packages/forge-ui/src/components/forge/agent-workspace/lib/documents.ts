import hljs from "highlight.js/lib/common"

// highlight.js names by file extension, for the canvas.
const LANGUAGES: Record<string, string> = {
  py: "python",
  ts: "typescript",
  tsx: "typescript",
  js: "javascript",
  jsx: "javascript",
  mjs: "javascript",
  json: "json",
  md: "markdown",
  html: "xml",
  xml: "xml",
  svg: "xml",
  css: "css",
  scss: "scss",
  sh: "bash",
  bash: "bash",
  zsh: "bash",
  yaml: "yaml",
  yml: "yaml",
  toml: "ini",
  ini: "ini",
  sql: "sql",
  go: "go",
  rs: "rust",
  java: "java",
  kt: "kotlin",
  rb: "ruby",
  php: "php",
  c: "c",
  h: "c",
  cpp: "cpp",
  cs: "csharp",
  swift: "swift",
}

export const extensionOf = (filename: string) =>
  filename.includes(".") ? filename.split(".").pop()!.toLowerCase() : ""

export const isMarkdown = (filename: string) =>
  ["md", "markdown", "mdx"].includes(extensionOf(filename))

/** The document's text as highlighted HTML (escaped by highlight.js). */
export function highlight(filename: string, text: string): string {
  const language = LANGUAGES[extensionOf(filename)]
  if (language && hljs.getLanguage(language))
    return hljs.highlight(text, { language, ignoreIllegals: true }).value
  if (extensionOf(filename) === "txt" || !extensionOf(filename))
    return hljs.highlight(text, { language: "plaintext" }).value
  return hljs.highlightAuto(text).value
}

const MIME: Record<string, string> = {
  md: "text/markdown",
  html: "text/html",
  json: "application/json",
  csv: "text/csv",
  svg: "image/svg+xml",
}

/** Saves the document to the person's downloads. */
export function downloadDocument(filename: string, text: string) {
  const type = MIME[extensionOf(filename)] ?? "text/plain"
  const url = URL.createObjectURL(new Blob([text], { type }))
  const link = Object.assign(document.createElement("a"), {
    href: url,
    download: filename.split("/").pop() || filename,
  })
  link.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}

/** An artifact's text: a text part, or base64 inline data. */
export function artifactText(data: {
  text?: string | undefined
  inlineData?: { data: string } | undefined
}): string | undefined {
  if (data.text !== undefined) return data.text
  if (!data.inlineData) return undefined
  const bytes = Uint8Array.from(atob(data.inlineData.data), (c) =>
    c.charCodeAt(0)
  )
  return new TextDecoder().decode(bytes)
}
