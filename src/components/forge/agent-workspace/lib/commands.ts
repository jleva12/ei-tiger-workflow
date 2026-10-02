import {
  Calculator01Icon,
  CheckListIcon,
  FileCodeIcon,
  Clock01Icon,
  DiceIcon,
  Note01Icon,
  NoteAddIcon,
  PlusSignIcon,
} from "@hugeicons/core-free-icons"

import type { AssistantCommand } from "@/components/forge/assistant"
import { useScreen } from "./screen-store"

/**
 * The workspace's `/` commands for the composer: a new chat, the side
 * panels, and prompts for the bundled tools (notes, dice, time, the
 * calculator). A command with `execute` acts at once; one with `prompt`
 * fills the composer with it (and sends it with `send`), followed by
 * anything already typed after the command. Add your own:
 * `commands={[...workspaceCommands, …]}`.
 */
export const workspaceCommands: AssistantCommand[] = [
  {
    id: "new",
    description: "Start a new conversation",
    icon: PlusSignIcon,
    execute: (aui) => aui.threads.switchToNewThread(),
  },
  {
    id: "notes",
    description: "Show or hide the notes panel",
    icon: Note01Icon,
    execute: () => useScreen.getState().togglePanel("notes"),
  },
  {
    id: "plan",
    description: "Show or hide the plan",
    icon: CheckListIcon,
    execute: () => useScreen.getState().togglePanel("plan"),
  },
  {
    id: "canvas",
    description: "Show or hide the canvas",
    icon: FileCodeIcon,
    execute: () => useScreen.getState().togglePanel("canvas"),
  },
  {
    id: "note",
    description: "Ask the assistant to save a note",
    icon: NoteAddIcon,
    prompt: "Add a note:",
  },
  {
    id: "dice",
    description: "Roll two dice",
    icon: DiceIcon,
    prompt: "Roll two 6-sided dice.",
    send: true,
  },
  {
    id: "time",
    description: "Ask the time somewhere",
    icon: Clock01Icon,
    prompt: "What time is it in",
  },
  {
    id: "calc",
    description: "Calculate an expression exactly",
    icon: Calculator01Icon,
    prompt: "Calculate",
  },
]
