import * as React from "react"
import { createStore } from "zustand"

import { createContextStore } from "@/lib/context-store"

/*
 * Whether the floating assistant's launcher shows (`app-assistant.tsx`). A
 * page with the agent built in, like Agent task, hides it while it's
 * mounted, so the launcher doesn't sit on top of the page's own composer.
 */

type AssistantLauncherState = {
  /** How many mounted components hide the launcher. */
  hiddenBy: number
  /** Hides the launcher until the returned function is called. */
  hide: () => () => void
}

export const {
  Provider: AssistantLauncherProvider,
  useStore: useAssistantLauncher,
} = createContextStore(
  () =>
    createStore<AssistantLauncherState>()((set) => ({
      hiddenBy: 0,
      hide: () => {
        set(({ hiddenBy }) => ({ hiddenBy: hiddenBy + 1 }))
        return () => set(({ hiddenBy }) => ({ hiddenBy: hiddenBy - 1 }))
      },
    })),
  { name: "AssistantLauncher" }
)

/** Hides the floating assistant's launcher while this component is mounted. */
export function useHideAssistantLauncher() {
  const hide = useAssistantLauncher((state) => state.hide)
  React.useEffect(() => hide(), [hide])
}
