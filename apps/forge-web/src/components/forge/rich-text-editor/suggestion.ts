import { ReactRenderer } from "@tiptap/react"
import type { SuggestionOptions } from "@tiptap/suggestion"

import {
  SuggestionMenu,
  type SuggestionMenuHandle,
  type SuggestionMenuProps,
  type SuggestionView,
} from "./suggestion-menu"

/**
 * The `render` option for a Tiptap suggestion: mounts `SuggestionMenu` next
 * to the caret and lets the plugin keep it positioned.
 */
export function suggestionRenderer<I>(
  view: SuggestionView<I>
): SuggestionOptions<I, I>["render"] {
  return () => {
    let renderer: ReactRenderer<
      SuggestionMenuHandle,
      SuggestionMenuProps
    > | null = null
    let unmount: (() => void) | undefined

    return {
      onStart: (props) => {
        renderer = new ReactRenderer(SuggestionMenu, {
          props: { ...props, view },
          editor: props.editor,
          className: "z-50",
        })
        unmount = props.mount(renderer.element as HTMLElement)
      },
      onUpdate: (props) => renderer?.updateProps({ ...props, view }),
      onKeyDown: ({ event }) => renderer?.ref?.onKeyDown(event) ?? false,
      onExit: () => {
        unmount?.()
        renderer?.destroy()
        renderer = null
      },
    }
  }
}
