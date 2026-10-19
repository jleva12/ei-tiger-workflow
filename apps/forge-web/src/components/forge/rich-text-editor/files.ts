import type { Editor } from "@tiptap/react"

import type { UploadHandler, UrlPolicy } from "./types"

export const IMAGE_TYPES = [
  "image/png",
  "image/jpeg",
  "image/gif",
  "image/webp",
  "image/avif",
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

/**
 * Uploads (or inlines) each image and inserts it at `pos`, or at the
 * selection. Files the editor can't hold are reported, not dropped silently.
 */
export async function insertFiles(
  editor: Editor,
  files: File[],
  {
    upload,
    maxFileSize,
    pos,
    urlPolicy,
    onError,
  }: {
    upload?: UploadHandler
    maxFileSize: number
    pos?: number
    urlPolicy: UrlPolicy
    onError: (message: string) => void
  }
) {
  let at = pos
  for (const file of files) {
    if (!IMAGE_TYPES.includes(file.type) || !editor.schema.nodes.image) {
      onError(`${file.name} isn't an image this editor accepts.`)
      continue
    }
    if (file.size > maxFileSize) {
      onError(`${file.name} is over the ${formatBytes(maxFileSize)} limit.`)
      continue
    }
    try {
      const src = upload ? await upload(file) : await readAsDataUrl(file)
      if (!urlPolicy(src, "image")) {
        onError(`${file.name} came back with a URL the editor doesn't allow.`)
        continue
      }
      const node = { type: "image", attrs: { src, alt: file.name } }
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
