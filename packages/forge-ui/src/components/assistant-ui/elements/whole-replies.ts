import { useEffect, useMemo, useRef, useState } from "react"

/*
 * Replies that arrived whole. An agent server that doesn't stream
 * (forge-agent-runtime's `with_streaming(False)`, or any ADK server running
 * with StreamingMode.NONE) sends each reply as one event once the model is
 * done. `useAdkAssistant` notes those replies as the run's events come in
 * (`noteWholeReply`), and `MarkdownText` writes each one in word by word with
 * `StreamingText`'s look (`useWrittenIn`), so it still reads as streamed.
 * Only once: written in, a reply is plain Markdown, also when its
 * conversation is opened again.
 */

/** On `MarkdownText`'s root while it writes a reply in. */
export const WRITING_IN_ATTRIBUTE = "data-aui-writing-in"

/** How long a word wears the gradient, as `StreamingText`'s `settleDelay`. */
export const SETTLE_DELAY = 300

// Noted and not written in yet. A reply that's never shown (its conversation
// closed first) gives way once there are more than LIMIT.
const waiting = new Set<string>()
const LIMIT = 20

/** A reply's text that arrived whole: `MarkdownText` writes it in once. */
export function noteWholeReply(text: string) {
  if (!text.trim()) return
  waiting.delete(text)
  waiting.add(text)
  for (const oldest of waiting) {
    if (waiting.size <= LIMIT) break
    waiting.delete(oldest)
  }
}

/** Whether `text` arrived whole and hasn't been written in yet. */
export const isWholeReply = (text: string) => waiting.has(text)

/** Reduced motion: the system's, or the person's Forge preference. */
function prefersReducedMotion() {
  if (typeof window === "undefined") return false
  return (
    document.documentElement.dataset.forgeMotion === "reduce" ||
    window.matchMedia?.("(prefers-reduced-motion: reduce)").matches === true
  )
}

// About 35 words a second, a fast model's pace; a long reply goes faster, so
// none takes more than MAX_MS. The words shown move on every TICK_MS.
const WORD_MS = 28
const MAX_MS = 6000
const TICK_MS = 32

const WORD = /\S+/g

/** Where each word of `text` ends: the reveal stops only after whole words. */
const wordEnds = (text: string) =>
  Array.from(text.matchAll(WORD), (match) => match.index + match[0].length)

/** How much of a reply `MarkdownText` shows while it writes it in. */
export type WritingIn = {
  /** Show `text.slice(0, end)`. */
  end: number
  /** Words are still coming: the stream head's cursor is shown. */
  writing: boolean
}

/**
 * Writes in a reply that arrived whole: `null` for any other text (one that
 * streamed, or history), or once it's written in and, `settledMs` later, its
 * last words have settled into the foreground. Reduced motion shows it whole
 * at once.
 */
export function useWrittenIn(
  text: string,
  settledMs: number
): WritingIn | null {
  const [reduceMotion] = useState(prefersReducedMotion)
  // Only the text the part first showed: a part whose text changes streamed.
  const [whole] = useState(() => (isWholeReply(text) ? text : null))
  const ends = useMemo(() => (whole === null ? [] : wordEnds(whole)), [whole])
  const [count, setCount] = useState(0)
  const [settled, setSettled] = useState(false)
  const startRef = useRef<number | null>(null)
  const active =
    whole !== null &&
    whole === text &&
    ends.length > 0 &&
    !reduceMotion &&
    !settled

  useEffect(() => {
    if (!active) return
    if (count >= ends.length) {
      waiting.delete(whole)
      const timer = setTimeout(() => setSettled(true), settledMs)
      return () => clearTimeout(timer)
    }
    // Scheduled against the start, so slow renders don't slow the pace.
    const start = (startRef.current ??= performance.now())
    const interval = Math.min(WORD_MS, MAX_MS / ends.length)
    const timer = setTimeout(() => {
      const due = Math.floor((performance.now() - start) / interval) + 1
      setCount(Math.min(ends.length, Math.max(count + 1, due)))
    }, TICK_MS)
    return () => clearTimeout(timer)
  }, [active, count, ends, whole, settledMs])

  if (!active) return null
  const writing = count < ends.length
  return {
    end: writing ? (ends[count - 1] ?? 0) : text.length,
    writing,
  }
}

type HastNode = {
  type: string
  tagName?: string
  value?: string
  properties?: Record<string, unknown>
  children?: HastNode[]
}

// Left as they are: code (the highlighter reads its text) and math (KaTeX's
// own markup).
const keepsItsText = (node: HastNode) => {
  if (node.tagName === "pre" || node.tagName === "code") return true
  const className = node.properties?.className
  return (
    Array.isArray(className) &&
    className.some((name) => String(name).startsWith("katex"))
  )
}

/**
 * A rehype plugin for a reply being written in: each word of its prose
 * becomes `<span data-streamed-word={index}>`, which `MarkdownText` draws as
 * `StreamedWord`, and with `cursor`, an `<i data-stream-cursor>` follows the
 * last one (its own tag, so it keeps its place, and its pulse, as words
 * arrive before it).
 */
export function rehypeStreamedWords({ cursor }: { cursor: boolean }) {
  return (tree: HastNode) => {
    let index = 0
    // Where the last word is: the cursor goes right after it.
    const head: { parent?: HastNode; at: number } = { at: 0 }
    const visit = (node: HastNode) => {
      if (!node.children || keepsItsText(node)) return
      const children: HastNode[] = []
      for (const child of node.children) {
        if (child.type !== "text" || !child.value) {
          visit(child)
          children.push(child)
          continue
        }
        // Words at even places, the spaces between them at odd ones.
        child.value.split(/(\s+)/).forEach((piece, place) => {
          if (!piece) return
          if (place % 2 === 1) {
            children.push({ type: "text", value: piece })
            return
          }
          children.push({
            type: "element",
            tagName: "span",
            properties: { dataStreamedWord: index++ },
            children: [{ type: "text", value: piece }],
          })
          head.parent = node
          head.at = children.length
        })
      }
      node.children = children
    }
    visit(tree)
    if (cursor && head.parent) {
      head.parent.children?.splice(head.at, 0, {
        type: "element",
        tagName: "i",
        properties: { dataStreamCursor: "" },
        children: [],
      })
    }
  }
}
