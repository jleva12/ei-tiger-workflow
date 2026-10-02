import * as React from "react"
import { Upload01Icon } from "@hugeicons/core-free-icons"
import { isValidTwitchUrl } from "@tiptap/extension-twitch"
import { isValidYoutubeUrl } from "@tiptap/extension-youtube"
import type { Editor } from "@tiptap/react"
import katex from "katex"

import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import {
  Field,
  FieldDescription,
  FieldError,
  FieldGroup,
  FieldLabel,
} from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { Textarea } from "@/components/ui/textarea"
import { Icon } from "../icon"
import { formatShortcut } from "./shortcuts"
import type {
  InsertDialogKind,
  InsertDialogRequest,
  RichTextActions,
} from "./types"

const COPY: Record<
  InsertDialogKind,
  { title: string; description: string; field: string; placeholder: string }
> = {
  link: {
    title: "Link",
    description: "Link the selected text to a page, file or email address.",
    field: "URL",
    placeholder: "https://example.com",
  },
  image: {
    title: "Image",
    description: "Upload an image, or link one that's already online.",
    field: "Image URL",
    placeholder: "https://example.com/diagram.png",
  },
  youtube: {
    title: "YouTube video",
    description: "Paste the link to a video, a Short or a playlist.",
    field: "Video URL",
    placeholder: "https://www.youtube.com/watch?v=…",
  },
  twitch: {
    title: "Twitch",
    description: "Paste the link to a channel, a video or a clip.",
    field: "Twitch URL",
    placeholder: "https://www.twitch.tv/videos/…",
  },
  audio: {
    title: "Audio",
    description: "Upload an audio file, or link one that's already online.",
    field: "Audio URL",
    placeholder: "https://example.com/episode.mp3",
  },
  inlineMath: {
    title: "Inline formula",
    description: "Write LaTeX; it renders inside the line of text.",
    field: "LaTeX",
    placeholder: "E = mc^2",
  },
  blockMath: {
    title: "Formula block",
    description: "Write LaTeX; it renders as a centred equation.",
    field: "LaTeX",
    placeholder: "\\int_0^\\infty e^{-x^2}\\,dx = \\frac{\\sqrt{\\pi}}{2}",
  },
}

/** Adds `https://` to bare domains; leaves mailto:, tel:, # and / links. */
function normalizeHref(value: string) {
  const href = value.trim()
  if (!href) return ""
  if (/^([a-z][a-z\d+.-]*:|#|\/)/i.test(href)) return href
  if (/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(href)) return `mailto:${href}`
  return `https://${href}`
}

function isHttpUrl(value: string) {
  try {
    return ["http:", "https:"].includes(new URL(value).protocol)
  } catch {
    return false
  }
}

function initialValues(editor: Editor, request: InsertDialogRequest) {
  const { from, to, empty } = editor.state.selection
  switch (request.kind) {
    case "link":
      return {
        value: (editor.getAttributes("link").href as string | undefined) ?? "",
        text: empty ? "" : editor.state.doc.textBetween(from, to, " "),
      }
    case "image":
      if (request.pos === undefined) return { value: "", text: "" }
      return {
        value: (editor.getAttributes("image").src as string | undefined) ?? "",
        text: (editor.getAttributes("image").alt as string | undefined) ?? "",
      }
    default:
      return { value: request.initial ?? "", text: "" }
  }
}

/**
 * The one dialog behind every insert that needs input: links, images,
 * embeds, audio and formulas. Saving writes to the editor and returns focus
 * to where the caret was.
 */
export function InsertDialog({
  editor,
  request,
  actions,
  onClose,
}: {
  editor: Editor
  request: InsertDialogRequest | null
  actions: RichTextActions
  onClose: () => void
}) {
  // Keep the last request while the dialog animates closed.
  const [shown, setShown] = React.useState(request)
  if (request && request !== shown) setShown(request)
  const kind = shown?.kind ?? "link"
  const copy = COPY[kind]
  const editing =
    shown?.pos !== undefined || (kind === "link" && editor.isActive("link"))

  const [value, setValue] = React.useState("")
  const [text, setText] = React.useState("")
  const [error, setError] = React.useState<string | null>(null)
  const [openedFor, setOpenedFor] = React.useState<InsertDialogRequest | null>(
    null
  )
  if (request && request !== openedFor) {
    const initial = initialValues(editor, request)
    setOpenedFor(request)
    setValue(initial.value)
    setText(initial.text)
    setError(null)
  }

  const isMath = kind === "inlineMath" || kind === "blockMath"
  const preview = React.useMemo(
    () =>
      isMath && value.trim()
        ? katex.renderToString(value, {
            throwOnError: false,
            displayMode: kind === "blockMath",
          })
        : "",
    [isMath, kind, value]
  )

  const pos = shown?.pos
  const id = React.useId()
  const chain = () => editor.chain().focus()

  const save = (): string | null => {
    const input = value.trim()
    switch (kind) {
      case "link": {
        const href = normalizeHref(input)
        if (!href) {
          chain().extendMarkRange("link").unsetLink().run()
          return null
        }
        if (editor.state.selection.empty && !editor.isActive("link")) {
          return chain()
            .insertContent({
              type: "text",
              text: text.trim() || href,
              marks: [{ type: "link", attrs: { href } }],
            })
            .run()
            ? null
            : "That link isn't allowed."
        }
        return chain().extendMarkRange("link").setLink({ href }).run()
          ? null
          : "That link isn't allowed."
      }
      case "image": {
        if (!isHttpUrl(input) && !input.startsWith("data:image/")) {
          return "Enter an http or https address."
        }
        const alt = text.trim() || null
        if (pos !== undefined) {
          chain()
            .setNodeSelection(pos)
            .updateAttributes("image", { src: input, alt })
            .run()
        } else
          chain()
            .setImage({ src: input, alt: alt ?? undefined })
            .run()
        return null
      }
      case "youtube":
        if (!isValidYoutubeUrl(input)) return "That isn't a YouTube link."
        chain().setYoutubeVideo({ src: input }).run()
        return null
      case "twitch":
        if (!isValidTwitchUrl(input)) return "That isn't a Twitch link."
        chain().setTwitchVideo({ src: input }).run()
        return null
      case "audio":
        if (!isHttpUrl(input)) return "Enter an http or https address."
        chain().setAudio({ src: input }).run()
        return null
      case "inlineMath":
        if (pos !== undefined) {
          if (!input) chain().deleteInlineMath({ pos }).run()
          else chain().updateInlineMath({ latex: input, pos }).run()
        } else if (input) chain().insertInlineMath({ latex: input }).run()
        return null
      case "blockMath":
        if (pos !== undefined) {
          if (!input) chain().deleteBlockMath({ pos }).run()
          else chain().updateBlockMath({ latex: input, pos }).run()
        } else if (input) chain().insertBlockMath({ latex: input }).run()
        return null
    }
  }

  const submit = (event: { preventDefault: () => void }) => {
    event.preventDefault()
    const problem = save()
    if (problem) setError(problem)
    else onClose()
  }

  const upload = kind === "image" || kind === "audio" ? kind : null
  const showText =
    kind === "link" && editor.state.selection.empty && !editor.isActive("link")
  const showAlt = kind === "image"

  return (
    <Dialog open={request !== null} onOpenChange={(open) => !open && onClose()}>
      <DialogContent
        className="sm:max-w-md"
        finalFocus={() => editor.view.dom as HTMLElement}
      >
        <form onSubmit={submit} className="grid gap-5" noValidate>
          <DialogHeader>
            <DialogTitle>
              {editing ? `Edit ${copy.title.toLowerCase()}` : copy.title}
            </DialogTitle>
            <DialogDescription>{copy.description}</DialogDescription>
          </DialogHeader>

          <FieldGroup>
            {upload && pos === undefined && (
              <>
                <Button
                  type="button"
                  variant="outline"
                  onClick={() => {
                    onClose()
                    actions.pickFiles(upload)
                  }}
                >
                  <Icon icon={Upload01Icon} data-icon="inline-start" />
                  Upload {upload === "image" ? "an image" : "an audio file"}
                </Button>
                <div className="flex items-center gap-3 text-2xs text-muted-foreground before:h-px before:flex-1 before:bg-border after:h-px after:flex-1 after:bg-border">
                  or link one
                </div>
              </>
            )}
            <Field data-invalid={Boolean(error)}>
              <FieldLabel htmlFor={`${id}-value`}>{copy.field}</FieldLabel>
              {isMath ? (
                <Textarea
                  id={`${id}-value`}
                  value={value}
                  placeholder={copy.placeholder}
                  spellCheck={false}
                  aria-invalid={Boolean(error)}
                  onChange={(event) => setValue(event.target.value)}
                  onKeyDown={(event) => {
                    if (
                      event.key === "Enter" &&
                      (event.metaKey || event.ctrlKey)
                    ) {
                      submit(event)
                    }
                  }}
                  className="min-h-20 font-mono text-xs"
                />
              ) : (
                <Input
                  id={`${id}-value`}
                  value={value}
                  placeholder={copy.placeholder}
                  type={kind === "link" ? "text" : "url"}
                  inputMode="url"
                  autoComplete="off"
                  spellCheck={false}
                  aria-invalid={Boolean(error)}
                  onChange={(event) => {
                    setValue(event.target.value)
                    setError(null)
                  }}
                />
              )}
              {isMath && (
                <FieldDescription>
                  {formatShortcut("Mod-Enter")} saves.
                  {pos !== undefined &&
                    " Leave it empty to remove the formula."}
                </FieldDescription>
              )}
              {kind === "link" && editing && (
                <FieldDescription>
                  Leave it empty to remove the link.
                </FieldDescription>
              )}
              {error && <FieldError>{error}</FieldError>}
            </Field>
            {(showText || showAlt) && (
              <Field>
                <FieldLabel htmlFor={`${id}-text`}>
                  {showAlt ? "Alt text" : "Text to show"}
                </FieldLabel>
                <Input
                  id={`${id}-text`}
                  value={text}
                  placeholder={
                    showAlt ? "What the image shows" : "Defaults to the URL"
                  }
                  onChange={(event) => setText(event.target.value)}
                />
                {showAlt && (
                  <FieldDescription>
                    Read aloud by screen readers and shown if the image can't
                    load.
                  </FieldDescription>
                )}
              </Field>
            )}
            {isMath && (
              <div
                aria-label="Preview"
                className="grid min-h-14 place-items-center overflow-x-auto rounded-(--radius-card) border border-border bg-muted/40 px-3 py-2 text-sm"
              >
                {preview ? (
                  <span dangerouslySetInnerHTML={{ __html: preview }} />
                ) : (
                  <span className="text-xs text-subtle">Preview</span>
                )}
              </div>
            )}
          </FieldGroup>

          <DialogFooter>
            <Button type="button" variant="outline" onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit">{editing ? "Save" : "Insert"}</Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
