import {
  CompositeAttachmentAdapter,
  SimpleImageAttachmentAdapter,
  SimpleTextAttachmentAdapter,
  type AttachmentAdapter,
  type CompleteAttachment,
  type PendingAttachment,
} from "@assistant-ui/react"

/*
 * What the composer's attach button takes, and how each file reaches an ADK
 * agent: images and documents as inline parts of the message, text files as
 * text. assistant-ui's own adapters do images and text; documents are below.
 */

/**
 * Documents ADK's model bridge sends on as files (to OpenAI through its
 * Files API), by type and by extension for browsers that leave the type out.
 */
const DOCUMENTS: Record<string, string> = {
  ".pdf": "application/pdf",
  ".doc": "application/msword",
  ".docx":
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  ".pptx":
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}

/** The most a document may be: it travels in the message, as base64. */
export const MAX_DOCUMENT_BYTES = 10 * 1024 * 1024

const megabytes = (bytes: number) => `${(bytes / 1024 / 1024).toFixed(1)} MB`

function typeOf(file: File) {
  if (file.type) return file.type
  const name = file.name.toLowerCase()
  const extension = Object.keys(DOCUMENTS).find((ext) => name.endsWith(ext))
  return extension ? DOCUMENTS[extension] : "application/octet-stream"
}

/**
 * A type carrying the file's name, `application/pdf; name="report.pdf"`: ADK's
 * web client keeps only a part's type and data, both ways, so this is how the
 * chat still names the file when the conversation loads again. The agent
 * runtime gives the model the bare type (forge_common's AttachmentsPlugin).
 */
export const typeWithName = (type: string, name: string) =>
  `${type}; name="${name.replace(/[\r\n]+/g, " ").replace(/["\\]/g, "\\$&")}"`

function dataUrl(file: File, signal?: AbortSignal) {
  return new Promise<string>((resolve, reject) => {
    const reader = new FileReader()
    const abort = () => reader.abort()
    signal?.addEventListener("abort", abort, { once: true })
    reader.onload = () => resolve(String(reader.result))
    reader.onerror = () => reject(reader.error)
    reader.onabort = () =>
      reject(signal?.reason ?? new DOMException("Aborted", "AbortError"))
    reader.onloadend = () => signal?.removeEventListener("abort", abort)
    reader.readAsDataURL(file)
  })
}

/** PDF, Word and PowerPoint files, sent whole as a file part of the message. */
export class DocumentAttachmentAdapter implements AttachmentAdapter {
  accept = [...Object.keys(DOCUMENTS), ...Object.values(DOCUMENTS)].join(",")

  async add({ file }: { file: File }): Promise<PendingAttachment> {
    if (file.size > MAX_DOCUMENT_BYTES)
      throw new Error(
        `${file.name} is ${megabytes(file.size)}: a document can be at most ${megabytes(MAX_DOCUMENT_BYTES)}.`
      )
    return {
      id: crypto.randomUUID(),
      type: "document",
      name: file.name,
      contentType: typeOf(file),
      file,
      status: { type: "requires-action", reason: "composer-send" },
    }
  }

  async send(
    attachment: PendingAttachment,
    options?: { signal?: AbortSignal }
  ): Promise<CompleteAttachment> {
    return {
      ...attachment,
      status: { type: "complete" },
      content: [
        {
          type: "file",
          data: await dataUrl(attachment.file, options?.signal),
          mimeType: typeWithName(
            attachment.contentType ?? typeOf(attachment.file),
            attachment.name
          ),
          filename: attachment.name,
        },
      ],
    }
  }

  async remove() {}
}

/** Images, text files and documents: what the composer takes by default. */
export const defaultAttachments = (): AttachmentAdapter =>
  new CompositeAttachmentAdapter([
    new SimpleImageAttachmentAdapter(),
    new SimpleTextAttachmentAdapter(),
    new DocumentAttachmentAdapter(),
  ])
