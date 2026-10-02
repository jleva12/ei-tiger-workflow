import type { ToolCallMessagePartComponent } from "@assistant-ui/react"

import { Button } from "@/components/ui/button"
import type { Note } from "../lib/notes"
import { useScreen } from "../lib/screen-store"
import { toolResult } from "../lib/tool-result"
import { useToolApproved, useToolFinished } from "../lib/use-tool-finished"

import { ToolFrame } from "./tool-frame"

function ShowNotes() {
  const openPanel = useScreen((state) => state.openPanel)
  return (
    <Button
      variant="link"
      size="xs"
      className="ms-6 h-auto w-fit p-0 text-xs text-muted-foreground hover:text-foreground"
      onClick={() => openPanel("notes")}
    >
      Show notes
    </Button>
  )
}

const added = (result: unknown) =>
  toolResult<{ added: Note }>(result).added !== undefined

export const AddNoteUI: ToolCallMessagePartComponent<{ text: string }> = (
  part
) => {
  const openPanel = useScreen((state) => state.openPanel)
  // Open the notes as the note lands: at once when the tool isn't gated, or
  // after approval. A gated call first finishes with ADK's "requires
  // confirmation" placeholder, which isn't a note.
  useToolFinished(part.status, () => {
    if (added(part.result)) openPanel("notes")
  })
  useToolApproved(part, added, () => openPanel("notes"))

  const decision = part.approval?.approved
  if (part.approval && decision === undefined)
    return (
      <ToolFrame
        part={{ ...part, status: { type: "running" } }}
        running={<>Waiting for your approval to add a note</>}
        label={null}
      />
    )
  // Declined: the cancelled look (a cross, struck through), not a tick.
  if (decision === false)
    return (
      <ToolFrame
        part={{ ...part, status: { type: "incomplete", reason: "cancelled" } }}
        running={null}
        label={<>Note not added</>}
      />
    )
  if (!added(part.result))
    return (
      <ToolFrame
        part={part}
        running={<>Adding a note</>}
        label={<>Couldn't add the note</>}
      />
    )
  return (
    <ToolFrame
      part={part}
      running={<>Adding a note</>}
      label={
        <>
          Noted <b>“{part.args.text}”</b>
        </>
      }
    >
      <ShowNotes />
    </ToolFrame>
  )
}

export const RemoveNoteUI: ToolCallMessagePartComponent<{ note_id: string }> = (
  part
) => {
  const openPanel = useScreen((state) => state.openPanel)
  useToolFinished(part.status, () => openPanel("notes"))
  const { removed, error } = toolResult<{ removed: Note; error: string }>(
    part.result
  )
  return (
    <ToolFrame
      part={part}
      running={<>Removing a note</>}
      label={
        error ? (
          <>{error}</>
        ) : (
          <>
            Removed <b>“{removed?.text ?? part.args.note_id}”</b>
          </>
        )
      }
    />
  )
}

export const ListNotesUI: ToolCallMessagePartComponent = (part) => {
  const { notes = [] } = toolResult<{ notes: Note[] }>(part.result)
  return (
    <ToolFrame
      part={part}
      running={<>Reading the notes</>}
      label={
        <>
          Read {notes.length} {notes.length === 1 ? "note" : "notes"}
        </>
      }
    />
  )
}
