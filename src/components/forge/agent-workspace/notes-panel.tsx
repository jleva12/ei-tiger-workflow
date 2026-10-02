import { Note01Icon } from "@hugeicons/core-free-icons"

import { PanelButton, SidePanel } from "./side-panel"
import { useNotes } from "./lib/notes"

/**
 * The conversation's notes, straight from the session state. The agent's
 * note tools change that state on the server and ADK streams each change
 * here with the tool's event, so the list updates as the agent writes, and
 * follows the conversation that's open.
 */
export function NotesPanel() {
  const notes = useNotes()
  return (
    <SidePanel panel="notes" title="Notes" icon={Note01Icon}>
      {notes.length === 0 ? (
        <p className="p-4 text-sm text-muted-foreground">
          No notes yet. Ask the assistant to note something down.
        </p>
      ) : (
        <ol className="flex min-h-0 flex-col gap-2 overflow-y-auto p-3">
          {notes.map((note) => (
            <li
              key={note.id}
              className="animate-in rounded-md border bg-card px-3 py-2 text-sm shadow-xs duration-300 fade-in slide-in-from-top-1"
            >
              {note.text}
            </li>
          ))}
        </ol>
      )}
    </SidePanel>
  )
}

/** In the top bar: shows or hides the notes, with how many there are. */
export function NotesButton() {
  const count = useNotes().length
  return (
    <PanelButton
      panel="notes"
      label="Notes"
      icon={Note01Icon}
      badge={count > 0 ? count : undefined}
    />
  )
}
