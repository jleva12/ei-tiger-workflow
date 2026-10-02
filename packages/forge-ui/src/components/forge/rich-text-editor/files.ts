import type { Editor, JSONContent } from "@tiptap/react"

import type { UploadHandler } from "./types"

export const IMAGE_TYPES = [
  "image/png",
  "image/jpeg",
  "image/gif",
  "image/webp",
  "image/avif",
  "image/svg+xml",
]

export const AUDIO_TYPES = [
  "audio/mpeg",
  "audio/mp4",
  "audio/aac",
  "audio/ogg",
  "audio/wav",
  "audio/webm",
  "audio/flac",
]

function readAsDataUrl(file: File) {
  return new Promise<string>((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(String(reader.result))
    reader.onerror = () => reject(reader.error ?? new Error("Unreadable file"))
    reader.readAsDataURL(file)
  })
}

function formatBytes(bytes: number) {
  if (bytes >= 1024 * 1024) return `${Math.round(bytes / 1024 / 1024)} MB`
  return `${Math.round(bytes / 1024)} KB`
}

function nodeFor(file: File, src: string): JSONContent | null {
  if (file.type.startsWith("image/")) {
    return { type: "image", attrs: { src, alt: file.name } }
  }
  if (file.type.startsWith("audio/")) return { type: "audio", attrs: { src } }
  return null
}

/**
 * Uploads (or inlines) each image and audio file and inserts it at `pos`, or
 * at the selection. Files the editor can't hold are reported, not dropped
 * silently.
 */
export async function insertFiles(
  editor: Editor,
  files: File[],
  {
    upload,
    maxFileSize,
    pos,
    onError,
  }: {
    upload?: UploadHandler
    maxFileSize: number
    pos?: number
    onError: (message: string) => void
  }
) {
  let at = pos
  for (const file of files) {
    const supported =
      (file.type.startsWith("image/") && editor.schema.nodes.image) ||
      (file.type.startsWith("audio/") && editor.schema.nodes.audio)
    if (!supported) {
      onError(`${file.name} isn't an image or audio file this editor accepts.`)
      continue
    }
    if (file.size > maxFileSize) {
      onError(`${file.name} is over the ${formatBytes(maxFileSize)} limit.`)
      continue
    }
    try {
      const src = upload ? await upload(file) : await readAsDataUrl(file)
      if (editor.isDestroyed) return
      const node = nodeFor(file, src)
      if (!node) continue
      if (at === undefined) {
        editor.chain().focus().insertContent(node).run()
      } else {
        // The document may have changed while the upload ran.
        at = Math.min(at, editor.state.doc.content.size)
        editor.chain().insertContentAt(at, node).focus().run()
        at = editor.state.selection.to
      }
    } catch (error) {
      onError(
        `${file.name} couldn't be added: ${error instanceof Error ? error.message : String(error)}`
      )
    }
  }
}
