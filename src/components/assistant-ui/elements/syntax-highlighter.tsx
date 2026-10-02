import * as React from "react"
import type { SyntaxHighlighterProps } from "@assistant-ui/react-markdown"

import "./syntax-highlighter.css"

// Highlighted blocks by language and code, so a block that remounts (a
// reply re-rendering, scrolling back) doesn't flash plain first.
const cache = new Map<string, string>()
// While a block streams, its code changes with each chunk: wait for a pause.
const DEBOUNCE_MS = 120

const NO_HIGHLIGHT = new Set(["", "text", "txt", "plaintext", "plain"])

/**
 * The inside of Shiki's `<code>`: lines of tokens, each coloured by the
 * `--shiki-light` and `--shiki-dark` variables (syntax-highlighter.css).
 * Shiki and its languages load on first use; undefined for a language it
 * doesn't know.
 */
async function highlight(language: string, code: string) {
  const key = `${language}\n${code}`
  const cached = cache.get(key)
  if (cached !== undefined) return cached
  const { codeToHtml } = await import("shiki")
  try {
    const html = await codeToHtml(code, {
      lang: language,
      themes: { light: "github-light-default", dark: "github-dark-default" },
      defaultColor: false,
    })
    const inner = /<code[^>]*>([\s\S]*)<\/code>/.exec(html)?.[1] ?? ""
    cache.set(key, inner)
    return inner
  } catch {
    return undefined
  }
}

/**
 * Fenced code in the agent's replies, highlighted by Shiki with GitHub's
 * light and dark themes, in the thread's own code block. It shows the plain
 * code until the highlighted version is ready, and for languages Shiki
 * doesn't know.
 */
export function SyntaxHighlighter({
  components: { Pre, Code },
  language,
  code,
}: SyntaxHighlighterProps) {
  const lang = language.toLowerCase()
  const key = `${lang}\n${code}`
  // The last highlight this block got: for its code now, or a moment ago
  // while it streams (shown until the new one is ready).
  const [latest, setLatest] = React.useState<string>()
  const cached = cache.get(key)
  const html = cached ?? latest

  React.useEffect(() => {
    // Only when this render had no highlight: another copy of the block (the
    // streamed reply and the final one) may have cached it since, which
    // `highlight` then returns at once.
    if (NO_HIGHLIGHT.has(lang) || cached !== undefined) return
    let current = true
    const timer = setTimeout(() => {
      void highlight(lang, code).then((result) => {
        if (current && result !== undefined) setLatest(result)
      })
    }, DEBOUNCE_MS)
    return () => {
      current = false
      clearTimeout(timer)
    }
  }, [lang, code, key, cached])

  const highlighted = html !== undefined && !NO_HIGHLIGHT.has(lang)
  return (
    // The markdown's `Pre` is memoized on its markdown node and ignores new
    // children, so it remounts (keyed by what it shows) as the code streams
    // and when its highlight arrives.
    <Pre key={highlighted ? html : code} className="aui-shiki">
      {highlighted ? (
        <code dangerouslySetInnerHTML={{ __html: html }} />
      ) : (
        <Code>{code}</Code>
      )}
    </Pre>
  )
}
