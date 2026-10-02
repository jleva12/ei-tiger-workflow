import { create } from "zustand"

import { useScreen } from "./screen-store"

type CanvasState = {
  /** The document the canvas shows; null for none. */
  filename: string | null
  /** The version it shows; null follows the latest as new ones are saved. */
  version: number | null
  openDocument: (filename: string, version?: number | null) => void
  showVersion: (version: number | null) => void
}

/**
 * Which of the agent's documents (ADK artifacts) the canvas shows. Opening
 * one also opens the canvas panel beside the chat.
 */
export const useCanvas = create<CanvasState>((set) => ({
  filename: null,
  version: null,
  openDocument: (filename, version = null) => {
    set({ filename, version })
    useScreen.getState().openPanel("canvas")
  },
  showVersion: (version) => set({ version }),
}))
