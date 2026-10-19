import { createStore } from "zustand"

import type { DashboardWidget } from "@/features/overview/lib/dashboard"
import { createContextStore } from "@/lib/context-store"

/*
 * Laying out a dashboard: while a person customizes it, their draft is what
 * the grid shows and what the toolbar saves. Done saves the draft, Cancel
 * drops it; nothing is saved while they work. Defining a widget of their
 * own (or changing one) opens the widget builder over it; the widgets their
 * organization shares open in its library.
 */

type LayoutEditorState = {
  /** The layout being edited; null when the dashboard isn't. */
  draft: DashboardWidget[] | null
  /** Start customizing from a layout. */
  edit: (widgets: DashboardWidget[]) => void
  change: (widgets: DashboardWidget[]) => void
  /** Stop customizing, keeping nothing of the draft. */
  close: () => void
  /** The widget in the builder: its ID, or null for a new one; null when closed. */
  building: { id: string | null } | null
  build: (id: string | null) => void
  stopBuilding: () => void
  /** The organization's shared widgets are open. */
  browsing: boolean
  browse: (open: boolean) => void
}

export const {
  Provider: LayoutEditorProvider,
  useStore: useLayoutEditor,
  useStoreApi: useLayoutEditorApi,
} = createContextStore(
  () =>
    createStore<LayoutEditorState>()((set) => ({
      draft: null,
      edit: (widgets) => set({ draft: widgets }),
      change: (widgets) => set({ draft: widgets }),
      close: () => set({ draft: null, building: null, browsing: false }),
      building: null,
      build: (id) => set({ building: { id } }),
      stopBuilding: () => set({ building: null }),
      browsing: false,
      browse: (browsing) => set({ browsing }),
    })),
  { name: "LayoutEditor" }
)

/** Bring a widget just added into view, once it's drawn. */
export function revealWidget(id: string) {
  const selector = `[data-widget-content="${CSS.escape(id)}"]`
  requestAnimationFrame(() =>
    requestAnimationFrame(() =>
      document.querySelector(selector)?.scrollIntoView({ block: "nearest" })
    )
  )
}
