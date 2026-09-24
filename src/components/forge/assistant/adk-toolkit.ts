import { defineToolkit } from "@assistant-ui/react"

import { AdkAuthRequestUI, AdkConfirmationUI, AdkInputRequestUI } from "./adk"

/** Renderers for ADK's human-in-the-loop calls. */
export const adkToolkit = defineToolkit({
  adk_request_confirmation: { type: "backend", render: AdkConfirmationUI },
  adk_request_credential: { type: "backend", render: AdkAuthRequestUI },
  adk_request_input: { type: "backend", render: AdkInputRequestUI },
})
