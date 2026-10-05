import type {
  AttachmentAdapter,
  CompleteAttachment,
  PendingAttachment,
} from "@assistant-ui/react"

/**
 * Files people can attach to a message (the composer's + button, or dropped
 * on it), sent to the agent inline with the message:
 *
 * - images, which the models see;
 * - text and code files, sent as text;
 * - PDFs, which the chat API turns into their text for models that don't
 *   read PDFs themselves (chat-api agents/attachments.py).
 *
 * The ADK runtime sends a file without its name, so each non-image file goes
 * with a "📎 name" line, which the agent reads and the message shows.
 */

/**
 * Sent before each non-image file as its own text part, "📎 name": the
 * runtime drops file names, so this is how the agent (and, after a reload,
 * the message) knows it.
 */
export const FILE_LABEL = "📎 "

/** The file name a "📎 name" part gives; undefined for any other text. */
export const labelledFileName = (text: string) =>
  text.startsWith(FILE_LABEL) && !text.includes("\n")
    ? text.slice(FILE_LABEL.length).trim() || undefined
    : undefined

type Kind = "image" | "text" | "pdf"

const IMAGE_TYPES = ["image/png", "image/jpeg", "image/gif", "image/webp"]

// Text the browser may not type as text/* (or at all).
const TEXT_EXTENSIONS = [
  "txt",
  "md",
  "markdown",
  "csv",
  "tsv",
  "json",
  "jsonl",
  "yaml",
  "yml",
  "toml",
  "ini",
  "xml",
  "html",
  "css",
  "scss",
  "js",
  "jsx",
  "mjs",
  "ts",
  "tsx",
  "py",
  "rb",
  "go",
  "rs",
  "java",
  "kt",
  "swift",
  "c",
  "h",
  "cpp",
  "cs",
  "php",
  "sh",
  "bash",
  "zsh",
  "sql",
  "log",
  "env",
  "gitignore",
  "dockerfile",
]

const MB = 1024 * 1024
const LIMITS: Record<Kind, number> = {
  image: 10 * MB,
  text: 2 * MB,
  pdf: 20 * MB,
}

const extensionOf = (name: string) =>
  name.includes(".") ? name.split(".").pop()!.toLowerCase() : name.toLowerCase()

function kindOf(file: File): Kind | undefined {
  if (IMAGE_TYPES.includes(file.type)) return "image"
  if (file.type === "application/pdf" || extensionOf(file.name) === "pdf")
    return "pdf"
  if (
    file.type.startsWith("text/") ||
    TEXT_EXTENSIONS.includes(extensionOf(file.name))
  )
    return "text"
  return undefined
}

/** The file's bytes as standard base64. */
async function base64Of(file: File): Promise<string> {
  const bytes = new Uint8Array(await file.arrayBuffer())
  let binary = ""
  // In chunks: spreading a large array into one call overflows the stack.
  for (let offset = 0; offset < bytes.length; offset += 0x8000)
    binary += String.fromCharCode(...bytes.subarray(offset, offset + 0x8000))
  return btoa(binary)
}

export class ChatAttachmentAdapter implements AttachmentAdapter {
  accept = [
    ...IMAGE_TYPES,
    "application/pdf",
    "text/*",
    ...TEXT_EXTENSIONS.map((extension) => `.${extension}`),
  ].join(",")

  async add({ file }: { file: File }): Promise<PendingAttachment> {
    const kind = kindOf(file)
    const base = {
      id: crypto.randomUUID(),
      type: kind === "image" ? "image" : "document",
      name: file.name,
      contentType: file.type,
      file,
    }
    // Shown on the file's chip; the message waits until it's removed.
    if (!kind)
      return {
        ...base,
        status: {
          type: "incomplete",
          reason: "error",
          message: "Only images, PDFs and text or code files can be attached",
        },
      }
    if (file.size > LIMITS[kind])
      return {
        ...base,
        status: {
          type: "incomplete",
          reason: "error",
          message: `Larger than ${LIMITS[kind] / MB} MB`,
        },
      }
    return {
      ...base,
      status: { type: "requires-action", reason: "composer-send" },
    }
  }

  async send(attachment: PendingAttachment): Promise<CompleteAttachment> {
    const { file } = attachment
    const kind = kindOf(file)!
    const data = await base64Of(file)
    const mimeType =
      kind === "image"
        ? file.type
        : kind === "pdf"
          ? "application/pdf"
          : // Text of any kind goes as text, which every model reads.
            file.type.startsWith("text/")
            ? file.type
            : "text/plain"
    return {
      ...attachment,
      status: { type: "complete" },
      content:
        kind === "image"
          ? [{ type: "image", image: `data:${mimeType};base64,${data}` }]
          : [
              { type: "text", text: `${FILE_LABEL}${file.name}` },
              { type: "file", data, mimeType, filename: file.name },
            ],
    }
  }

  async remove(): Promise<void> {}
}
