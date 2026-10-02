import * as React from "react"
import { cn } from "cn"

import { Skeleton } from "@/components/ui/skeleton"
import type { RichTextEditorProps } from "./rich-text-editor"

// Tiptap, KaTeX, the emoji set and the syntax grammars come to ~500 kB
// gzipped, so the editor loads with the first field that shows it, not with
// every page that could.
const load = () => import("./rich-text-editor")
const Editor = React.lazy(() =>
  load().then((module) => ({ default: module.RichTextEditor }))
)
const View = React.lazy(() =>
  load().then((module) => ({ default: module.RichTextView }))
)

function EditorSkeleton({
  className,
  toolbar,
}: {
  className?: string
  toolbar: boolean
}) {
  return (
    <div
      aria-busy="true"
      aria-label="Loading editor"
      className={cn(
        "flex flex-col rounded-(--radius-control) border border-border bg-background",
        className
      )}
    >
      {toolbar && (
        <div className="flex items-center gap-1.5 border-b border-border px-2 py-2">
          <Skeleton className="h-5 w-12" />
          <Skeleton className="h-5 w-24" />
          <Skeleton className="h-5 w-40" />
        </div>
      )}
      <div className="flex min-h-32 flex-col gap-2 px-4 py-3">
        <Skeleton className="h-4 w-2/3" />
        <Skeleton className="h-4 w-1/2" />
      </div>
    </div>
  )
}

/**
 * A complete rich text field on Tiptap. See `rich-text-editor.tsx` for the
 * props; this wrapper loads the editor on first render and shows a field
 * skeleton meanwhile.
 */
export function RichTextEditor(props: RichTextEditorProps) {
  const readOnly = Boolean(props.readOnly || props.disabled)
  return (
    <React.Suspense
      fallback={
        <EditorSkeleton
          className={props.className}
          toolbar={props.toolbar ?? !readOnly}
        />
      }
    >
      <Editor {...props} />
    </React.Suspense>
  )
}

/** Saved rich text, shown read-only; loads the editor like `RichTextEditor`. */
export function RichTextView(props: React.ComponentProps<typeof View>) {
  return (
    <React.Suspense
      fallback={
        <div aria-busy="true" className="flex flex-col gap-2">
          <Skeleton className="h-4 w-2/3" />
          <Skeleton className="h-4 w-1/2" />
        </div>
      }
    >
      <View {...props} />
    </React.Suspense>
  )
}
