import {
  createContext,
  useContext,
  type ComponentType,
  type ReactNode,
} from "react"
import type {
  AdkAuthCredential,
  AdkAuthRequest,
} from "@assistant-ui/react-google-adk"

import type { AssistantCommand } from "./commands"
import type { ModelSettings } from "./model-settings"
import type { AssistantSourcesConfig } from "./sources"
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

/**
 * How the approval card presents one gated tool (ADK `require_confirmation`),
 * in place of its name and raw arguments. Each field falls back to a generic
 * one.
 */
export type ApprovalView = {
  /** e.g. "Add this note?" */
  title?: ReactNode
  /** What approving does, in a sentence. */
  description?: ReactNode
  /** What will happen, drawn from the call's arguments. */
  preview?: ComponentType<{ args: Record<string, unknown> }>
  /** The approve button's label; "Approve" by default. */
  approveLabel?: string
  /** Shown once approved; "Approved" by default. */
  approvedLabel?: ReactNode
  /** Shown once denied; "Denied" by default. */
  deniedLabel?: ReactNode
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
  /** Shown at the top of the composer; see `AssistantOptions`. */
  composerContext?: ReactNode | undefined
  /** Persistent task progress above the composer. */
  composerLead?: ComponentType | undefined
  /** Controls beside the model section; see `AssistantOptions`. */
  composerActions?: ReactNode | undefined
  /** Right-aligned controls immediately before Send. */
  composerTrailingActions?: ComponentType | undefined
  /** Opens an artifact from the top bar's menu; see `AssistantOptions`. */
  onOpenArtifact?: ((name: string) => void) | undefined
  /** Above the composer after a reply; see `AssistantOptions`. */
  followUps?: ComponentType | undefined
  /** Beside each reply's actions; see `AssistantOptions`. */
  messageMeta?: ComponentType | undefined
  /** How gated tools ask for approval, by tool name; see `AssistantOptions`. */
  approvals?: Readonly<Record<string, ApprovalView>> | undefined
  /** How answers cite what the tools found; see `AssistantOptions`. */
  sources?: AssistantSourcesConfig | undefined
  /** What the app's agents are called; see `AssistantOptions`. */
  agents?: Readonly<Record<string, string>> | undefined
}

export const AssistantSettingsContext = createContext<AssistantSettings>({})

export const useAssistantSettings = () => useContext(AssistantSettingsContext)
