import { useAdkSessionState } from "@assistant-ui/react-google-adk"

export type Note = { id: string; text: string }

/**
 * The open conversation's notes, which its agent keeps in the session state
 * `notes` (adk_chat.builtin_tools). Updates as each change streams in.
 */
export function useNotes(): Note[] {
  const state = useAdkSessionState()
  return Array.isArray(state.notes) ? (state.notes as Note[]) : []
}
