import { TextSelection } from "@tiptap/pm/state"
import { useEditorState, type Editor } from "@tiptap/react"
import { cn } from "cn"

interface Heading {
  pos: number
  level: number
  text: string
}

function readHeadings(editor: Editor) {
  const headings: Heading[] = []
  editor.state.doc.descendants((node, pos) => {
    if (node.type.name !== "heading") return node.isBlock
    headings.push({
      pos,
      level: node.attrs.level as number,
      text: node.textContent,
    })
    return false
  })
  return headings
}

/**
 * The document's headings as a table of contents, read from the document
 * itself so it never writes anchors into the content. The section holding
 * the caret is marked; choosing a heading moves the caret there.
 */
export function OutlinePanel({
  editor,
  className,
}: {
  editor: Editor
  className?: string
}) {
  const { headings, current } = useEditorState({
    editor,
    selector: ({ editor }) => {
      if (editor.isDestroyed) return { headings: [], current: null }
      const headings = readHeadings(editor)
      const caret = editor.state.selection.from
      const current = headings.findLast((heading) => heading.pos < caret)
      return { headings, current: current?.pos ?? null }
    },
  })
  const top = Math.min(...headings.map((heading) => heading.level))

  const go = (heading: Heading) => {
    editor
      .chain()
      .focus()
      .command(({ tr }) => {
        tr.setSelection(TextSelection.near(tr.doc.resolve(heading.pos + 1)))
        return true
      })
      .run()
    // Scroll the editor's own content area, not the page around it.
    const dom = editor.view.nodeDOM(heading.pos)
    const scroller = editor.view.dom.closest("[data-slot=rich-text-scroll]")
    if (!(dom instanceof HTMLElement) || !scroller) return
    if (scroller.scrollHeight > scroller.clientHeight) {
      scroller.scrollTop +=
        dom.getBoundingClientRect().top -
        scroller.getBoundingClientRect().top -
        12
    } else dom.scrollIntoView({ block: "nearest" })
  }

  return (
    <nav
      aria-label="Outline"
      data-slot="rich-text-outline"
      className={cn(
        "flex w-[min(14rem,40%)] shrink-0 flex-col gap-1 overflow-y-auto border-l border-border px-2 py-3",
        className
      )}
    >
      <div className="px-2 pb-1 text-2xs font-medium text-muted-foreground">
        Outline
      </div>
      {headings.length === 0 ? (
        <p className="px-2 text-xs text-subtle">
          Headings you add appear here.
        </p>
      ) : (
        <ul className="flex flex-col">
          {headings.map((heading) => (
            <li key={heading.pos}>
              <button
                type="button"
                aria-current={heading.pos === current ? "location" : undefined}
                onClick={() => go(heading)}
                style={{
                  paddingLeft: `${0.5 + (heading.level - top) * 0.75}rem`,
                }}
                className={cn(
                  "flex w-full rounded-(--radius-item) py-1 pr-2 text-left text-xs text-muted-foreground hover:bg-muted hover:text-foreground aria-[current]:bg-muted aria-[current]:font-medium aria-[current]:text-foreground",
                  heading.level === top && "text-foreground"
                )}
              >
                <span className="line-clamp-2">
                  {heading.text || "Untitled"}
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </nav>
  )
}
