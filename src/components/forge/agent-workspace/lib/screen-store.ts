import { create } from "zustand"

/** The side panels the screen can show beside the chat, one at a time. */
export type Panel = "notes" | "plan" | "canvas"

type ScreenState = {
  /** The panel showing beside the chat; null for none. */
  panel: Panel | null
  openPanel: (panel: Panel) => void
  closePanel: () => void
  togglePanel: (panel: Panel) => void
  /** Progress strips dismissed in this browser session, keyed by response. */
  dismissedProgress: readonly string[]
  dismissProgress: (id: string) => void
}

/**
 * What the screen shows around the chat. Tool UIs change it when their calls
 * finish (`useToolFinished`), e.g. opening the notes panel after `add_note`;
 * the top bar's buttons change it too.
 */
export const useScreen = create<ScreenState>((set) => ({
  panel: null,
  dismissedProgress: [],
  dismissProgress: (id) =>
    set((state) => ({
      dismissedProgress: [
        ...state.dismissedProgress.filter((key) => key !== id).slice(-99),
        id,
      ],
    })),
  openPanel: (panel) => set({ panel }),
  closePanel: () => set({ panel: null }),
  togglePanel: (panel) =>
    set((state) => ({ panel: state.panel === panel ? null : panel })),
}))
