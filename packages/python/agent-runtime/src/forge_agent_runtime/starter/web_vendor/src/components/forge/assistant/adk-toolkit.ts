import { defineToolkit } from "@assistant-ui/react"

import {
  AdkAuthRequestUI,
  AdkConfirmationUI,
  AdkDelegation,
  AdkInputRequestUI,
} from "./adk"

/**
 * Renderers for ADK's own calls: the human-in-the-loop ones, and
 * `finish_task`, which a task agent (`mode="task"`) calls when it's done.
 * Its report reaches the person as the result of the call that handed it
 * the request, so it isn't shown.
 */
export const adkToolkit = defineToolkit({
  adk_request_confirmation: { type: "backend", render: AdkConfirmationUI },
  adk_request_credential: { type: "backend", render: AdkAuthRequestUI },
  adk_request_input: { type: "backend", render: AdkInputRequestUI },
  finish_task: { type: "backend", render: () => null },
})

/**
 * Renderers for the calls handing a request to one of the app's agents
 * (`agents`), each shown as asking it (`AdkDelegation`).
 */
export const delegationToolkit = (agents: Readonly<Record<string, string>>) =>
  defineToolkit(
    Object.fromEntries(
      Object.keys(agents).map((name) => [
        name,
        { type: "backend" as const, render: AdkDelegation },
      ])
    )
  )
