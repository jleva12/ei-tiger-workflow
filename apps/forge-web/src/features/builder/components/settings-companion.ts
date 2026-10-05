import * as React from "react"

import { withViewTransition } from "./utils"

/** The card beside a settings dialog's card (settings-dialog.tsx). */
export type CompanionSlot = {
  /** Where the card beside the settings renders. */
  slot: HTMLElement | null
  /** It's open: Escape or a click outside closes it first. */
  open: boolean
  setOpen: (open: boolean) => void
}

export const CompanionSlotContext = React.createContext<CompanionSlot | null>(
  null
)

/**
 * Opens and closes the card beside the settings, gliding: `[open, setOpen]`.
 * Inside a `SettingsDialog` only.
 */
export function useSettingsCompanion(): [boolean, (open: boolean) => void] {
  const context = React.useContext(CompanionSlotContext)
  if (!context) throw new Error("useSettingsCompanion needs a SettingsDialog")
  const { open, setOpen } = context
  const toggle = React.useCallback(
    (next: boolean) => withViewTransition(() => setOpen(next)),
    [setOpen]
  )
  return [open, toggle]
}
