/*
 * Care for what a file brings into the page: its links, and its text.
 */

const SAFE_PROTOCOLS = new Set(["http:", "https:", "mailto:", "tel:"])

/**
 * Makes the links a renderer drew from a file safe to follow: web, mail and
 * phone links open in a new tab without access to this one; in-document
 * anchors stay; anything else (javascript:, data:, file:) loses its target.
 */
export function hardenLinks(root: HTMLElement) {
  for (const link of root.querySelectorAll<HTMLAnchorElement>("a[href]")) {
    const href = link.getAttribute("href") ?? ""
    if (href.startsWith("#")) continue
    let url: URL | undefined
    try {
      url = new URL(href, "https://invalid.example/")
    } catch {
      url = undefined
    }
    if (
      !url ||
      !SAFE_PROTOCOLS.has(url.protocol) ||
      url.host === "invalid.example"
    ) {
      link.removeAttribute("href")
      link.setAttribute("aria-disabled", "true")
      continue
    }
    link.target = "_blank"
    link.rel = "noopener noreferrer nofollow"
  }
}

/** Past this, text previews show the start of the file. */
export const TEXT_PREVIEW_LIMIT = 8 * 1024 * 1024

/**
 * A file's text: UTF-8 (with or without a byte-order mark) or UTF-16 by its
 * mark, else Windows-1252, which reads any byte. Null for a binary file.
 */
export async function readText(
  data: Blob,
  limit = TEXT_PREVIEW_LIMIT
): Promise<{ text: string; truncated: boolean } | null> {
  const truncated = data.size > limit
  const bytes = new Uint8Array(await data.slice(0, limit).arrayBuffer())
  const utf16 =
    bytes[0] === 0xff && bytes[1] === 0xfe
      ? "utf-16le"
      : bytes[0] === 0xfe && bytes[1] === 0xff
        ? "utf-16be"
        : undefined
  if (utf16) return { text: new TextDecoder(utf16).decode(bytes), truncated }
  // NUL bytes mean it isn't text.
  const sample = bytes.subarray(0, 8192)
  if (sample.includes(0)) return null
  try {
    // A cut at the limit may split a character; decode leniently there.
    const text = new TextDecoder("utf-8", { fatal: !truncated }).decode(bytes)
    return { text, truncated }
  } catch {
    return { text: new TextDecoder("windows-1252").decode(bytes), truncated }
  }
}

/*
 * Office draws bullets and ticks with Symbol and Wingdings, whose glyphs
 * sit in the Private Use Area (U+F0B7 for Symbol's bullet); browsers have no
 * font for them and draw empty boxes. These are the usual ones, as Unicode.
 */
const SYMBOL_GLYPHS: Record<string, string> = {
  "": "•",
  "": "▪",
  "": "■",
  "": "❑",
  "": "❖",
  "": "➢",
  "": "→",
  "": "➔",
  "": "✓",
  "": "✗",
  "": "◆",
  "": "◊",
  "": "−",
  "": "●",
  "": "○",
}
const PRIVATE_SYMBOLS = /[-]/g
const SYMBOL_FONTS = /font-family:\s*"?(?:Symbol|Wingdings[^;"]*)"?\s*;?/gi

/**
 * Shows the Symbol and Wingdings bullets and ticks a renderer drew from an
 * Office file as the Unicode characters they stand for: in its styles
 * (list markers) and its text.
 */
export function readableSymbols(styles: HTMLElement, body: HTMLElement) {
  const swap = (text: string) =>
    text.replace(PRIVATE_SYMBOLS, (glyph) => SYMBOL_GLYPHS[glyph] ?? "•")
  for (const style of styles.querySelectorAll("style")) {
    const css = style.textContent ?? ""
    if (!PRIVATE_SYMBOLS.test(css)) continue
    PRIVATE_SYMBOLS.lastIndex = 0
    style.textContent = swap(css).replace(SYMBOL_FONTS, "")
  }
  const walker = document.createTreeWalker(body, NodeFilter.SHOW_TEXT)
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    const text = node.nodeValue ?? ""
    if (PRIVATE_SYMBOLS.test(text)) {
      PRIVATE_SYMBOLS.lastIndex = 0
      node.nodeValue = swap(text)
    }
  }
}
