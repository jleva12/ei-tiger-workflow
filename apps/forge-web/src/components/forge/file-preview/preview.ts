import * as React from "react"
import {
  Doc01Icon,
  FileEmpty01Icon,
  Flowchart01Icon,
  Image01Icon,
  Pdf01Icon,
  Ppt01Icon,
  Txt01Icon,
  Xls01Icon,
} from "@hugeicons/core-free-icons"

import type { IconProp } from "@/components/forge/icons"
import type { PreviewKind } from "./kinds"

export const KIND_ICONS: Record<PreviewKind, IconProp> = {
  pdf: Pdf01Icon,
  word: Doc01Icon,
  slides: Ppt01Icon,
  sheet: Xls01Icon,
  markdown: Txt01Icon,
  text: Txt01Icon,
  mermaid: Flowchart01Icon,
  image: Image01Icon,
  none: FileEmpty01Icon,
}

/** What each renderer is given. */
export type ViewProps = {
  data: Blob
  name: string
  mediaType?: string
  /** The host's controls, at the end of the toolbar. */
  toolbarEnd?: React.ReactNode
  /** Offered when the file can't be shown after all. */
  onDownload?: () => void
}

const UNITS = ["B", "KB", "MB", "GB"]

export function formatSize(bytes: number) {
  let value = bytes
  let unit = 0
  while (value >= 1000 && unit < UNITS.length - 1) {
    value /= 1000
    unit += 1
  }
  const digits = unit === 0 || value >= 100 ? 0 : 1
  return `${value.toFixed(digits).replace(/\.0$/, "")} ${UNITS[unit]}`
}

/**
 * Something slow a renderer does with the file (parse, lay out), with its
 * result or its failure. Redone when `deps` change: until the new run
 * settles it's loading again, and a run that's been overtaken (or whose
 * view went) is dropped.
 */
export function usePreviewTask<T>(
  task: (signal: AbortSignal) => Promise<T>,
  deps: readonly unknown[]
): { value?: T; error?: Error; loading: boolean } {
  const [settled, setSettled] = React.useState<{
    deps: readonly unknown[]
    value?: T
    error?: Error
  }>()
  React.useEffect(() => {
    const controller = new AbortController()
    task(controller.signal).then(
      (value) => {
        if (!controller.signal.aborted) setSettled({ deps, value })
      },
      (error: unknown) => {
        if (controller.signal.aborted) return
        setSettled({
          deps,
          error: error instanceof Error ? error : new Error(String(error)),
        })
      }
    )
    return () => controller.abort()
    // The caller's deps decide when the task is redone.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)
  const current =
    settled !== undefined &&
    settled.deps.length === deps.length &&
    settled.deps.every((each, index) => Object.is(each, deps[index]))
  return current
    ? { value: settled.value, error: settled.error, loading: false }
    : { loading: true }
}

/** A friendlier line for a library's failure to read a file. */
export function readFailure(kind: PreviewKind, error: Error | undefined) {
  const text = error?.message ?? ""
  if (/password|encrypt/i.test(text))
    return "It's password-protected, so it can't be shown here."
  if (/zip|corrupt|invalid|unexpected end|end of central directory/i.test(text))
    return `It doesn't look like a valid ${KIND_NAMES[kind]} file. It may be damaged, or saved in another format under this name.`
  return text || "The browser couldn't read it."
}

const KIND_NAMES: Record<PreviewKind, string> = {
  pdf: "PDF",
  word: "Word",
  slides: "PowerPoint",
  sheet: "spreadsheet",
  markdown: "Markdown",
  text: "text",
  mermaid: "Mermaid",
  image: "image",
  none: "",
}
