import { defineToolkit } from "@assistant-ui/react"

import type { ApprovalView } from "@/components/forge/assistant"

import { AskUserUI } from "./ask-user"
import { CalculateUI, CurrentTimeUI, RollDiceUI } from "./basic-tools"
import { ReadDocumentUI, WriteDocumentUI } from "./document-tools"
import { NotePreview } from "./note-preview"
import { AddNoteUI, ListNotesUI, RemoveNoteUI } from "./note-tools"
import { SetPlanUI, UpdatePlanStepUI } from "./plan-tools"

/**
 * How the chat shows the chat API's tools (adk_chat.builtin_tools), by tool
 * name. Each is a backend tool: the server runs it; these only render the
 * call. A tool without an entry here gets the standard card.
 */
export const toolUIs = defineToolkit({
  calculate: { type: "backend", render: CalculateUI },
  roll_dice: { type: "backend", render: RollDiceUI },
  get_current_time: { type: "backend", render: CurrentTimeUI },
  add_note: { type: "backend", render: AddNoteUI },
  remove_note: { type: "backend", render: RemoveNoteUI },
  list_notes: { type: "backend", render: ListNotesUI },
  set_plan: { type: "backend", render: SetPlanUI },
  update_plan_step: { type: "backend", render: UpdatePlanStepUI },
  write_document: { type: "backend", render: WriteDocumentUI },
  read_document: { type: "backend", render: ReadDocumentUI },
  // Replaces the assistant's basic card for ADK's question requests.
  adk_request_input: { type: "backend", render: AskUserUI },
})

/**
 * How the tools that need the person's approval ask for it (chat-api:
 * `require_confirmation=True`), by tool name. A gated tool without an entry
 * gets a generic card with its name and arguments.
 */
export const approvalViews: Record<string, ApprovalView> = {
  add_note: {
    title: "Add this note?",
    description:
      "The assistant wants to save it to this conversation's notes panel.",
    preview: NotePreview,
    approveLabel: "Add note",
    approvedLabel: "You approved adding the note",
    deniedLabel: "You declined the note",
  },
}
