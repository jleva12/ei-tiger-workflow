import { createElement, type ComponentType, type ReactNode } from "react"
import {
  defaultGenerativeUILibrary,
  type GenerativeUILibrary,
} from "@assistant-ui/generative-ui/react"
import { RICH_ANSWER_VOCABULARY, type RichAnswerComponent } from "./catalog"
import { isText } from "./value"

type Component = GenerativeUILibrary[string]

/** Show the package field's screen-reader label above its control as well. */
export function labelled(own: Component): Component {
  const render = own.render as (props: Record<string, unknown>) => ReactNode
  return {
    ...own,
    render: (props: Record<string, unknown>) => {
      const control = render(props)
      if (!isText(props.label)) return control
      return createElement(
        "div",
        { "data-slot": "rich-field", className: "flex min-w-0 flex-col gap-1" },
        // The control names itself; this is for the eye.
        createElement(
          "span",
          {
            "aria-hidden": true,
            className: "text-2xs font-medium text-muted-foreground",
          },
          props.label
        ),
        control
      )
    },
  } as Component
}

/** Bind a Forge renderer to its schema and the package's streaming behavior. */
export function forgeComponent(
  name: RichAnswerComponent,
  Draw: ComponentType<Record<string, unknown>>
): Component {
  return {
    ...RICH_ANSWER_VOCABULARY[name],
    streamProperties: defaultGenerativeUILibrary[name]?.streamProperties,
    // Internal package keys ($status, $dispatch…) stay out of Forge's pieces.
    render: (props: Record<string, unknown>) =>
      createElement(
        Draw,
        Object.fromEntries(
          Object.entries(props).filter(([key]) => !key.startsWith("$"))
        )
      ),
  } as Component
}
