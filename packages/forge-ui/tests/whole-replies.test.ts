import assert from "node:assert/strict"
import { test } from "node:test"
import {
  isWholeReply,
  noteWholeReply,
  rehypeStreamedWords,
} from "../src/components/assistant-ui/elements/whole-replies.ts"

type Node = {
  type: string
  tagName?: string
  value?: string
  properties?: Record<string, unknown>
  children?: Node[]
}

const text = (value: string): Node => ({ type: "text", value })
const element = (
  tagName: string,
  children: Node[],
  properties: Record<string, unknown> = {}
): Node => ({ type: "element", tagName, properties, children })

/** The tree as words, spaces and the cursor, to read at a glance. */
function shape(node: Node): unknown {
  if (node.type === "text") return node.value
  if (node.properties?.dataStreamedWord !== undefined)
    return `word ${node.properties.dataStreamedWord}: ${node.children?.[0]?.value}`
  if (node.properties?.dataStreamCursor !== undefined) return "cursor"
  return { [node.tagName ?? node.type]: node.children?.map(shape) ?? [] }
}

test("a reply that arrived whole is written in once, the latest replies kept", () => {
  noteWholeReply("Deployed main to staging.")
  noteWholeReply("   ")
  assert.equal(isWholeReply("Deployed main to staging."), true)
  assert.equal(isWholeReply("   "), false)
  assert.equal(isWholeReply("Something it never said."), false)
  for (let index = 0; index < 20; index++) noteWholeReply(`Reply ${index}.`)
  assert.equal(isWholeReply("Deployed main to staging."), false)
  assert.equal(isWholeReply("Reply 0."), true)
  assert.equal(isWholeReply("Reply 19."), true)
})

test("each word of the prose is wrapped, its spaces kept, numbered in order", () => {
  const tree = element("root", [
    element("p", [
      text("Deployed "),
      element("strong", [text("main")]),
      text(" to\nstaging."),
    ]),
    text("\n"),
    element("ul", [element("li", [text("Two checks")])]),
  ])
  rehypeStreamedWords({ cursor: false })(tree)
  assert.deepEqual(shape(tree), {
    root: [
      {
        p: [
          "word 0: Deployed",
          " ",
          { strong: ["word 1: main"] },
          " ",
          "word 2: to",
          "\n",
          "word 3: staging.",
        ],
      },
      "\n",
      { ul: [{ li: ["word 4: Two", " ", "word 5: checks"] }] },
    ],
  })
})

test("code and math keep their text", () => {
  const tree = element("root", [
    element("p", [
      text("Run "),
      element("code", [text("make up")]),
      text(" then "),
      element("span", [text("x + y")], { className: ["katex"] }),
    ]),
    element("pre", [element("code", [text("npm run build")])]),
  ])
  rehypeStreamedWords({ cursor: false })(tree)
  assert.deepEqual(shape(tree), {
    root: [
      {
        p: [
          "word 0: Run",
          " ",
          { code: ["make up"] },
          " ",
          "word 1: then",
          " ",
          { span: ["x + y"] },
        ],
      },
      { pre: [{ code: ["npm run build"] }] },
    ],
  })
})

test("the cursor follows the last word, wherever it is", () => {
  const tree = element("root", [
    element("p", [text("Two checks")]),
    element("ul", [
      element("li", [text("slow "), element("em", [text("one")])]),
    ]),
    element("pre", [element("code", [text("not words")])]),
  ])
  rehypeStreamedWords({ cursor: true })(tree)
  assert.deepEqual(shape(tree), {
    root: [
      { p: ["word 0: Two", " ", "word 1: checks"] },
      {
        ul: [{ li: ["word 2: slow", " ", { em: ["word 3: one", "cursor"] }] }],
      },
      { pre: [{ code: ["not words"] }] },
    ],
  })

  const empty = element("root", [element("p", [])])
  rehypeStreamedWords({ cursor: true })(empty)
  assert.deepEqual(shape(empty), { root: [{ p: [] }] })
})
