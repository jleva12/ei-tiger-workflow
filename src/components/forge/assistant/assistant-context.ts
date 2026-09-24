import { createContext, useContext, type ReactNode } from "react"
import type {
  AdkAuthCredential,
  AdkAuthRequest,
} from "@assistant-ui/react-google-adk"

import type { AssistantCommand } from "./commands"
import type { ModelSettings } from "./model-settings"
import type { AdkArtifactsApi } from "./use-adk-assistant"

/**
 * Completes an ADK auth request, e.g. by opening `authUri` in a popup and
 * resolving with `{ authType: "oauth2", oauth2: { authResponseUri } }` once
 * the provider redirects back. Throw to leave the request pending.
 */
export type AuthRequestHandler = (
  request: AdkAuthRequest & { authUri: string | undefined }
) => Promise<AdkAuthCredential>

export type AssistantWelcome = {
  title?: ReactNode
  description?: ReactNode
}

export type AssistantSettings = {
  onAuthRequest?: AuthRequestHandler | undefined
  artifacts?: AdkArtifactsApi | undefined
  welcome?: AssistantWelcome | undefined
  /** The screen's title; names the agent until ADK reports one. */
  title?: string | undefined
  /** Whether the composer shows the model section. */
  showModels?: boolean | undefined
  modelSettings?: ModelSettings | undefined
  commands?: AssistantCommand[] | undefined
}

export const AssistantSettingsContext = createContext<AssistantSettings>({})

export const useAssistantSettings = () => useContext(AssistantSettingsContext)
