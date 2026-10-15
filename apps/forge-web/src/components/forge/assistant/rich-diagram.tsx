import * as React from "react"

import { drawMermaid } from "@/components/forge/file-preview/mermaid-draw"
import { useDarkTheme } from "@/components/forge/file-preview/layout"
import { Skeleton } from "@/components/ui/skeleton"

/**
 * A rich answer's Diagram: Mermaid source drawn in the app's theme, as the
 * file preview draws it (sanitized labels, no click handlers), fitted to
 * the chat down to 70% of its own size; one wider than that scrolls. Source that doesn't parse
 * is shown as it is. Loaded only when an answer has a diagram.
 */
export default function RichDiagram({
  code,
  title,
}: {
  code: string
  title?: string
}) {
  const dark = useDarkTheme()
  // What was drawn, and of what: a drawing of other source is still going.
  const source = `${dark ? "dark" : "light"}\n${code}`
  const [made, setMade] = React.useState<{
    source: string
    svg?: string
    width?: number
  }>()
  React.useEffect(() => {
    let current = true
    drawMermaid(code, dark).then(
      ({ svg, size }) =>
        current && setMade({ source, svg, width: size?.width }),
      () => current && setMade({ source })
    )
    return () => {
      current = false
    }
  }, [code, dark, source])
  const drawn = made?.source === source ? made : undefined
  return (
    <figure data-slot="rich-diagram" className="flex min-w-0 flex-col gap-1.5">
      {drawn === undefined ? (
        <Skeleton className="h-32 w-full" />
      ) : drawn.svg !== undefined ? (
        // Fitted, down to 70% of its own size (Mermaid draws large), so its
        // labels stay legible: wider than that, it scrolls.
        <div className="overflow-x-auto" tabIndex={0}>
          <div
            role="img"
            aria-label={title || "Diagram"}
            className="mx-auto [&_svg]:h-auto [&_svg]:w-full [&_svg]:max-w-none"
            style={
              drawn.width
                ? {
                    width: `max(100%, ${Math.round(drawn.width * 0.7)}px)`,
                    maxWidth: `${drawn.width}px`,
                  }
                : undefined
            }
            // Mermaid's own output, drawn with securityLevel "strict".
            dangerouslySetInnerHTML={{ __html: drawn.svg }}
          />
        </div>
      ) : (
        <pre className="overflow-x-auto rounded-(--radius-item) bg-muted p-2.5 font-mono text-2xs">
          {code}
        </pre>
      )}
      {title && (
        <figcaption className="text-2xs text-muted-foreground">
          {title}
        </figcaption>
      )}
    </figure>
  )
}
