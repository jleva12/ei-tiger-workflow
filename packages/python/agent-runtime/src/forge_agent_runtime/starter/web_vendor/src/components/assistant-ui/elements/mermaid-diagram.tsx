import * as React from "react"
import type { SyntaxHighlighterProps } from "@assistant-ui/react-markdown"

// A diagram streams in as incomplete code: draw once it pauses.
const DEBOUNCE_MS = 400
let renders = 0

/** Whether the app is in dark mode (the theme provider's `dark` class). */
function useDarkMode() {
  const read = () => document.documentElement.classList.contains("dark")
  const [dark, setDark] = React.useState(read)
  React.useEffect(() => {
    const observer = new MutationObserver(() => setDark(read()))
    observer.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["class"],
    })
    return () => observer.disconnect()
  }, [])
  return dark
}

/** The diagram as SVG; throws when the code isn't a valid diagram (yet). */
async function draw(code: string, dark: boolean) {
  const { default: mermaid } = await import("mermaid")
  mermaid.initialize({
    startOnLoad: false,
    // Diagram text is escaped and scripts and links are off.
    securityLevel: "strict",
    theme: dark ? "dark" : "neutral",
    fontFamily: "inherit",
  })
  const id = `aui-mermaid-${++renders}`
  try {
    const { svg } = await mermaid.render(id, code)
    return svg
  } finally {
    // A failed render leaves its scratch element behind.
    document.getElementById(`d${id}`)?.remove()
  }
}

/**
 * A ```mermaid block in the agent's replies, drawn as a diagram (flowcharts,
 * sequence diagrams and the rest). Its code shows while it streams or when it
 * can't be drawn, and on request.
 */
export function MermaidDiagram({
  components: { Pre, Code },
  code,
}: SyntaxHighlighterProps) {
  const dark = useDarkMode()
  const [svg, setSvg] = React.useState<string>()
  const [failed, setFailed] = React.useState(false)
  const [showCode, setShowCode] = React.useState(false)

  React.useEffect(() => {
    let current = true
    const timer = setTimeout(() => {
      draw(code, dark)
        .then((result) => {
          if (!current) return
          setSvg(result)
          setFailed(false)
        })
        .catch(() => current && setFailed(true))
    }, DEBOUNCE_MS)
    return () => {
      current = false
      clearTimeout(timer)
    }
  }, [code, dark])

  if (!svg || showCode)
    return (
      <div className="relative">
        <Pre>
          <Code>{code}</Code>
        </Pre>
        {svg ? (
          <button
            type="button"
            onClick={() => setShowCode(false)}
            className="absolute end-2 top-2 rounded-md bg-background/80 px-2 py-0.5 text-2xs text-muted-foreground hover:text-foreground"
          >
            Show diagram
          </button>
        ) : failed ? (
          <p className="absolute end-3 top-2 text-2xs text-muted-foreground">
            Drawing diagram…
          </p>
        ) : null}
      </div>
    )

  return (
    <div className="aui-mermaid relative overflow-x-auto rounded-b-(--radius-band) border border-t-0 border-border bg-background p-4">
      <div
        className="flex justify-center [&_svg]:h-auto [&_svg]:max-w-full"
        dangerouslySetInnerHTML={{ __html: svg }}
      />
      <button
        type="button"
        onClick={() => setShowCode(true)}
        className="absolute end-2 top-2 rounded-md px-2 py-0.5 text-2xs text-muted-foreground hover:bg-muted hover:text-foreground"
      >
        Code
      </button>
    </div>
  )
}
