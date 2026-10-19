import assert from "node:assert/strict"
import { describe, test } from "node:test"

import {
  closeOpenMarkdown,
  parseMarkdown,
  serializeMarkdown,
  tidyBlankLines,
} from "../src/components/forge/rich-text-editor/core/markdown.ts"
import {
  defaultUrlPolicy,
  sanitizeContent,
} from "../src/components/forge/rich-text-editor/core/security.ts"
import { makeEditor } from "./rich-text-fixtures.ts"

/** Node types (and marks) anywhere in a document, for structure checks. */
function typesIn(json: {
  type?: string
  marks?: { type: string }[]
  content?: unknown[]
}): Set<string> {
  const found = new Set<string>()
  const visit = (node: typeof json) => {
    if (node.type) found.add(node.type)
    for (const mark of node.marks ?? []) found.add(`mark:${mark.type}`)
    for (const child of (node.content ?? []) as (typeof json)[]) visit(child)
  }
  visit(json)
  return found
}

/**
 * Import → check structure → export → re-import, and the second import
 * must equal the first (and export the same Markdown again).
 */
function roundTrip(markdown: string, expected: string[], mentions = false) {
  const first = makeEditor(markdown, { mentions })
  const json = first.getJSON()
  const types = typesIn(json)
  for (const type of expected) {
    assert.ok(types.has(type), `expected ${type} in ${[...types].join(", ")}`)
  }
  const exported = serializeMarkdown(first)
  const second = makeEditor(exported, { mentions })
  assert.deepEqual(second.getJSON(), json, `re-import of:\n${exported}`)
  assert.equal(serializeMarkdown(second), exported)
  return exported
}

describe("Markdown round trips", () => {
  const cases: [string, string, string[]][] = [
    ["headings", "# One\n\n## Two\n\n###### Six", ["heading"]],
    ["bold and italic", "**bold** and *italic*", ["mark:bold", "mark:italic"]],
    ["strikethrough", "~~gone~~", ["mark:strike"]],
    ["underline", "++under++", ["mark:underline"]],
    ["inline code", "use `npm test`", ["mark:code"]],
    ["highlight", "==important==", ["mark:highlight"]],
    [
      "subscript and superscript",
      "H<sub>2</sub>O and x<sup>2</sup>",
      ["mark:subscript", "mark:superscript"],
    ],
    ["links", '[site](https://example.com "Title")', ["mark:link"]],
    ["blockquotes", "> quoted\n> text", ["blockquote"]],
    ["bulleted lists", "- a\n- b\n  - nested", ["bulletList", "listItem"]],
    ["numbered lists", "1. one\n2. two", ["orderedList"]],
    ["checklists", "- [ ] todo\n- [x] done", ["taskList", "taskItem"]],
    ["code blocks", "```ts\nconst a = 1\n```", ["codeBlock"]],
    ["dividers", "above\n\n---\n\nbelow", ["horizontalRule"]],
    [
      "tables",
      "| a | b |\n| --- | --- |\n| 1 | 2 |",
      ["table", "tableHeader", "tableCell"],
    ],
    ["images", "![A diagram](https://example.com/a.png)", ["image"]],
    ["inline math", "Mass $E=mc^2$ here", ["inlineMath"]],
    ["block math", "$$\nx^2\n$$", ["blockMath"]],
    ["hard breaks", "line one  \nline two", ["hardBreak"]],
  ]
  for (const [name, markdown, expected] of cases) {
    test(name, () => {
      roundTrip(markdown, expected)
    })
  }

  test("mentions", () => {
    roundTrip('Ask [@ id="ada" label="Ada"] first', ["mention"], true)
  })

  test("a table cell's paragraphs come back as one line with breaks", () => {
    const editor = makeEditor("| a |\n| --- |\n| x |")
    editor.commands.insertContentAt(editor.state.doc.content.size - 3, {
      type: "paragraph",
      content: [{ type: "text", text: "y" }],
    })
    const exported = serializeMarkdown(editor)
    assert.match(exported, /x<br>y/)
  })

  test("emoji are saved as the emoji character", () => {
    const editor = makeEditor("")
    editor.commands.setContent({
      type: "doc",
      content: [
        {
          type: "paragraph",
          content: [
            { type: "text", text: "Nice " },
            { type: "emoji", attrs: { name: "smile" } },
          ],
        },
      ],
    })
    assert.equal(serializeMarkdown(editor), "Nice 😄")
  })

  test("an empty document is an empty string", () => {
    assert.equal(serializeMarkdown(makeEditor("")), "")
  })
})

describe("syntax for disabled features stays literal", () => {
  const minimal = (markdown: string) =>
    makeEditor(markdown, { preset: "minimal" })

  for (const [name, markdown] of [
    ["a table", "| a | b |\n| --- | --- |\n| 1 | 2 |"],
    ["a heading", "# Title"],
    ["a code block", "```ts\nconst a = 1\n```"],
    ["a checklist", "- [ ] todo\n- [x] done"],
    ["an image", "See ![alt](https://example.com/a.png) here"],
  ]) {
    test(`${name} survives the round trip`, () => {
      const editor = minimal(markdown)
      assert.ok(typesIn(editor.getJSON()).has("mark:markdownLiteral"))
      assert.equal(serializeMarkdown(editor), markdown)
    })
  }

  test("another editor's syntax doesn't leak into this one", () => {
    // A full editor registers the math, highlight and mention tokenizers.
    makeEditor("$x$ ==y==", { mentions: true })
    const editor = minimal("Cost $5 and ==mark==")
    assert.equal(serializeMarkdown(editor), "Cost $5 and ==mark==")
  })

  test("inline formatting of a disabled mark keeps its markup", () => {
    const editor = makeEditor("**bold** text", { features: { bold: false } })
    assert.equal(serializeMarkdown(editor), "**bold** text")
  })
})

describe("unsafe URLs", () => {
  test("the default policy", () => {
    assert.equal(defaultUrlPolicy("https://example.com", "link"), true)
    assert.equal(defaultUrlPolicy("/docs/page", "link"), true)
    assert.equal(defaultUrlPolicy("#top", "link"), true)
    assert.equal(defaultUrlPolicy("mailto:a@b.co", "link"), true)
    assert.equal(defaultUrlPolicy("javascript:alert(1)", "link"), false)
    assert.equal(defaultUrlPolicy("JaVa\nScript:alert(1)", "link"), false)
    assert.equal(defaultUrlPolicy("data:text/html,<b>x</b>", "link"), false)
    assert.equal(
      defaultUrlPolicy("data:image/png;base64,iVBORw0KGgo=", "image"),
      true
    )
    assert.equal(
      defaultUrlPolicy("data:image/svg+xml;base64,PHN2Zz4=", "image"),
      false
    )
    assert.equal(defaultUrlPolicy("vbscript:x", "image"), false)
  })

  test("links lose an unsafe href but keep their text", () => {
    const editor = makeEditor("")
    const { doc, removed } = parseMarkdown(
      editor,
      "[click](javascript:alert(1)) me"
    )
    assert.equal(removed, 1)
    assert.equal(JSON.stringify(doc).includes("javascript"), false)
    assert.match(JSON.stringify(doc), /click/)
  })

  test("unsafe images become their alt text", () => {
    const { content, removed } = sanitizeContent({
      type: "doc",
      content: [
        { type: "image", attrs: { src: "javascript:x", alt: "Chart" } },
      ],
    })
    assert.equal(removed, 1)
    assert.deepEqual(content.content, [
      { type: "paragraph", content: [{ type: "text", text: "Chart" }] },
    ])
  })
})

describe("tidyBlankLines", () => {
  test("empty paragraphs don't pile up blank lines", () => {
    assert.equal(tidyBlankLines("One\n\n\n\nTwo\n\n"), "One\n\nTwo")
    assert.equal(tidyBlankLines("\n\nOne"), "One")
  })
  test("code fences and math blocks keep their blank lines", () => {
    const code = "```py\na = 1\n\n\nb = 2\n```"
    assert.equal(tidyBlankLines(code), code)
    const math = "$$\na\n\n\nb\n$$"
    assert.equal(tidyBlankLines(math), math)
  })
  test("one empty paragraph adds no blank lines", () => {
    const editor = makeEditor("One")
    editor.commands.insertContentAt(editor.state.doc.content.size, [
      { type: "paragraph" },
      { type: "paragraph", content: [{ type: "text", text: "Two" }] },
    ])
    assert.equal(serializeMarkdown(editor), "One\n\nTwo")
  })
  test("nor do several (Tiptap's &nbsp; spacer doesn't survive a reload)", () => {
    const editor = makeEditor("One")
    editor.commands.insertContentAt(editor.state.doc.content.size, [
      { type: "paragraph" },
      { type: "paragraph" },
      { type: "paragraph", content: [{ type: "text", text: "Two" }] },
    ])
    assert.equal(serializeMarkdown(editor), "One\n\nTwo")
  })
})

describe("closeOpenMarkdown", () => {
  test("closes a fence a stream left open", () => {
    assert.equal(
      closeOpenMarkdown("Intro\n```ts\nconst a"),
      "Intro\n```ts\nconst a\n```"
    )
  })
  test("leaves closed fences alone", () => {
    const done = "```\ncode\n```\nafter"
    assert.equal(closeOpenMarkdown(done), done)
  })
  test("closes an open $$ block", () => {
    assert.equal(closeOpenMarkdown("$$\nx^2"), "$$\nx^2\n$$")
  })
})
