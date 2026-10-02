import * as React from "react"
import {
  CaseSensitiveIcon,
  RegexIcon,
  ReplaceAllIcon,
  ReplaceIcon,
  WholeWordIcon,
} from "@hugeicons/core-free-icons"
import { useEditorState, type Editor } from "@tiptap/react"

import { Input } from "@/components/ui/input"
import { ToolbarAction, ToolbarToggle } from "./controls"

/**
 * ⌘F search over the document: matches highlight as you type, Enter and
 * ⇧Enter step through them, and Replace swaps the current one or all.
 */
export function FindReplaceBar({
  editor,
  onClose,
}: {
  editor: Editor
  onClose: () => void
}) {
  const search = useEditorState({
    editor,
    selector: ({ editor }) => {
      const storage = editor.storage.findAndReplace
      if (!storage) return null
      return {
        total: storage.results.length,
        current: storage.currentIndex,
        caseSensitive: storage.caseSensitive,
        useRegex: storage.useRegex,
        wholeWord: storage.wholeWord,
      }
    },
  })
  // Searching the selected text is the usual way in.
  const [initialTerm] = React.useState(() => {
    const { from, to, empty } = editor.state.selection
    const selected = empty ? "" : editor.state.doc.textBetween(from, to, " ")
    return selected && !selected.includes("\n") && selected.length < 200
      ? selected
      : (editor.storage.findAndReplace?.searchTerm ?? "")
  })
  const [term, setTerm] = React.useState(initialTerm)
  const [replacement, setReplacement] = React.useState(
    () => editor.storage.findAndReplace?.replaceTerm ?? ""
  )
  const findRef = React.useRef<HTMLInputElement>(null)
  const readOnly = !editor.isEditable

  React.useEffect(() => {
    if (initialTerm) editor.commands.setSearchTerm(initialTerm)
    findRef.current?.focus()
    findRef.current?.select()
  }, [editor, initialTerm])

  if (!search) return null

  const close = () => {
    editor.commands.clearSearch()
    onClose()
    editor.commands.focus()
  }

  const onKeyDown = (event: React.KeyboardEvent) => {
    if (event.key === "Escape") {
      event.preventDefault()
      close()
    }
  }

  const count = !term
    ? ""
    : search.total === 0
      ? "No results"
      : `${search.current === null ? "–" : search.current + 1} of ${search.total}`

  return (
    <div
      role="search"
      aria-label="Find and replace"
      data-slot="rich-text-find"
      onKeyDown={onKeyDown}
      className="flex flex-wrap items-center gap-x-2 gap-y-1 border-b border-border bg-muted/40 px-2 py-1.5"
    >
      <div className="flex min-w-56 flex-1 items-center gap-0.5">
        <Input
          ref={findRef}
          aria-label="Find"
          placeholder="Find"
          value={term}
          onChange={(event) => {
            setTerm(event.target.value)
            editor.commands.setSearchTerm(event.target.value)
          }}
          onKeyDown={(event) => {
            if (event.key !== "Enter") return
            event.preventDefault()
            if (event.shiftKey) editor.commands.goToPreviousResult()
            else editor.commands.goToNextResult()
          }}
          className="h-7 min-w-0 flex-1 text-xs md:text-xs"
        />
        <span
          aria-live="polite"
          className="w-[4.5rem] shrink-0 text-center text-2xs text-muted-foreground tabular-nums"
        >
          {count}
        </span>
        <ToolbarToggle
          icon={CaseSensitiveIcon}
          label="Match case"
          pressed={search.caseSensitive}
          onPressedChange={() =>
            editor.commands.setCaseSensitive(!search.caseSensitive)
          }
        />
        <ToolbarToggle
          icon={WholeWordIcon}
          label="Whole words"
          pressed={search.wholeWord}
          onPressedChange={() =>
            editor.commands.setWholeWord(!search.wholeWord)
          }
        />
        <ToolbarToggle
          icon={RegexIcon}
          label="Regular expression"
          pressed={search.useRegex}
          onPressedChange={() => editor.commands.setUseRegex(!search.useRegex)}
        />
        <ToolbarAction
          icon="up"
          label="Previous match"
          shortcut="Shift-Enter"
          disabled={!search.total}
          onClick={() => editor.commands.goToPreviousResult()}
        />
        <ToolbarAction
          icon="down"
          label="Next match"
          shortcut="Enter"
          disabled={!search.total}
          onClick={() => editor.commands.goToNextResult()}
        />
      </div>
      {!readOnly && (
        <div className="flex min-w-56 flex-1 items-center gap-0.5">
          <Input
            aria-label="Replace with"
            placeholder="Replace with"
            value={replacement}
            onChange={(event) => {
              setReplacement(event.target.value)
              editor.commands.setReplaceTerm(event.target.value)
            }}
            onKeyDown={(event) => {
              if (event.key !== "Enter") return
              event.preventDefault()
              if (event.metaKey || event.ctrlKey) editor.commands.replaceAll()
              else editor.commands.replace()
            }}
            className="h-7 min-w-0 flex-1 text-xs md:text-xs"
          />
          <ToolbarAction
            icon={ReplaceIcon}
            label="Replace"
            shortcut="Enter"
            disabled={!search.total}
            onClick={() => editor.commands.replace()}
          />
          <ToolbarAction
            icon={ReplaceAllIcon}
            label="Replace all"
            shortcut="Mod-Enter"
            disabled={!search.total}
            onClick={() => editor.commands.replaceAll()}
          />
        </div>
      )}
      <ToolbarAction
        icon="close"
        label="Close"
        shortcut="Escape"
        onClick={close}
      />
    </div>
  )
}
