/*
 * The agent workspace: everything around the assistant's conversation that
 * makes a capable agent's work legible — task progress by the composer, the
 * plan, notes and canvas panels, response and context statistics, a chat
 * history palette, follow-up questions, a tools menu and the tool cards for
 * the chat API's built-in tools (adk-chat). Mount it through
 * `AssistantScreen`'s slots; see `useAgentWorkspace`.
 */

// The agent, connected
export {
  useAgentWorkspace,
  type AgentConnection,
  type AgentWorkspaceOptions,
  type ModelSection,
} from "./lib/use-agent-workspace"
export { workspaceCommands } from "./lib/commands"

// Around the composer
export { ProgressDock } from "./progress-dock"
export { ContextMeter } from "./context-meter"
export { ToolsMenu } from "./tools-menu"
export { ChatHistoryPalette } from "./chat-history-palette"
export { FollowUps } from "./follow-ups"

// Beside each reply
export { MessageStats } from "./message-stats"
export { TurnDetails } from "./turn-details"
export { StatsList } from "./stats-list"

// Side panels and their top bar buttons
export { SidePanel, PanelButton } from "./side-panel"
export { PlanPanel, PlanButton } from "./plan-panel"
export { PlanSteps, StepMark } from "./plan-steps"
export { NotesPanel, NotesButton } from "./notes-panel"
export { CanvasPanel } from "./canvas-panel"
export { ActivityMark } from "./activity-mark"

// Tool cards
export { toolUIs, approvalViews } from "./tool-uis"
export { ToolFrame } from "./tool-uis/tool-frame"
export { AskUserUI } from "./tool-uis/ask-user"

// State and helpers
export { useScreen, type Panel } from "./lib/screen-store"
export { useCanvas } from "./lib/canvas"
export { usePlan, finishedSteps, type Plan, type StepStatus } from "./lib/plan"
export { useNotes, type Note } from "./lib/notes"
export { useFollowUps } from "./lib/follow-ups"
export { useAgentTools, type AgentTool } from "./lib/agent-tools"
export {
  useAssistantModels,
  toAssistantModel,
  modelMakerIcon,
  type AgentModel,
  type AgentModels,
} from "./lib/assistant-models"
export { useChatHistory, type HistoryEntry } from "./lib/chat-history"
export {
  useTurnStats,
  useTurnStatsSync,
  conversationStats,
  type ConversationStats,
  type TurnStats,
} from "./lib/turn-stats"
export { useAgentActivity } from "./lib/use-agent-activity"
export {
  activityForTurn,
  activityLabels,
  toolLabel,
  type ActivityItem,
  type ActivityState,
} from "./lib/activity"
export { useToolFinished, useToolApproved } from "./lib/use-tool-finished"
export { toolResult } from "./lib/tool-result"
