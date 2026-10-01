/*
 * Citation markers in an agent's answer: "[S3]", or "[S1, S3]", each naming a
 * passage by the ref its tool result gave it (see sources.tsx).
 */

/** "[S3]", or several at once: "[S1, S3]". */
const MARKER = /\[([A-Z]{1,3}\d{1,4}(?:\s*,\s*[A-Z]{1,3}\d{1,4})*)\]/g
export const REF_HREF = "#source-"

const refsIn = (marker: string) => marker.split(",").map((ref) => ref.trim())

/** The refs a text cites, first citation first. */
export function citedRefs(text: string): string[] {
  const refs = new Set<string>()
  for (const match of text.matchAll(MARKER)) {
    for (const ref of refsIn(match[1] ?? "")) refs.add(ref)
  }
  return [...refs]
}

type MdNode = {
  type: string
  value?: string
  url?: string
  children?: MdNode[]
}

/**
 * A remark plugin: each "[S3]" in the text becomes a link to `#source-S3`,
 * which `SourceAnchor` draws as a citation. Code and links are left alone.
 */
export function remarkSourceRefs() {
  return (tree: MdNode) => linkRefs(tree)
}

function linkRefs(node: MdNode): void {
  if (!node.children || node.type === "link" || node.type === "linkReference")
    return
  const children: MdNode[] = []
  for (const child of node.children) {
    const text = child.type === "text" ? (child.value ?? "") : ""
    let last = 0
    for (const match of text.matchAll(MARKER)) {
      const start = match.index ?? 0
      if (start > last) {
        // The space before a citation doesn't break: it stays with its word.
        const before = text.slice(last, start).replace(/ $/, "\u00a0")
        children.push({ type: "text", value: before })
      }
      for (const ref of refsIn(match[1] ?? "")) {
        children.push({
          type: "link",
          url: `${REF_HREF}${ref}`,
          children: [{ type: "text", value: `[${ref}]` }],
        })
      }
      last = start + match[0].length
    }
    if (last === 0) {
      linkRefs(child)
      children.push(child)
    } else if (last < text.length) {
      children.push({ type: "text", value: text.slice(last) })
    }
  }
  node.children = children
}
