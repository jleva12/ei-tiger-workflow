import type { ThreadMessage, ToolCallMessagePart } from "@assistant-ui/react"

export type ActivityState =
  "running" | "waiting" | "complete" | "error" | "stopped"
export type ActivityItem = {
  id: string
  label: string
  state: ActivityState
  tool: ToolCallMessagePart
}

export const activityLabels: Record<ActivityState, string> = {
  running: "Working",
  waiting: "Needs your input",
  complete: "Complete",
  error: "Needs attention",
  stopped: "Stopped",
}

const names: Record<string, string> = {
  set_plan: "Created a plan",
  update_plan_step: "Updated the plan",
  calculate: "Calculate",
  get_current_time: "Check the time",
  roll_dice: "Roll dice",
  add_note: "Save a note",
  list_notes: "Read notes",
  remove_note: "Remove a note",
  write_document: "Write a document",
  read_document: "Read a document",
  request_input: "Ask a question",
  adk_request_confirmation: "Request approval",
  adk_request_credential: "Request sign-in",
}

export function readResult(value: unknown): Record<string, unknown> {
  if (typeof value === "string") {
    try {
      value = JSON.parse(value)
    } catch {
      return {}
    }
  }
  if (!value || typeof value !== "object" || Array.isArray(value)) return {}
  const result = value as Record<string, unknown>
  return Object.keys(result).length === 1 && "result" in result
    ? readResult(result.result)
    : result
}

export function toolLabel(tool: ToolCallMessagePart) {
  const result = readResult(tool.result)
  if (tool.toolName === "update_plan_step") {
    const step = result.text
    const verb =
      result.status === "done"
        ? "Completed"
        : result.status === "in_progress"
          ? "Started"
          : result.status === "skipped"
            ? "Skipped"
            : "Reopened"
    return typeof step === "string"
      ? `${verb}: ${step}`
      : `Update step ${tool.args.step ?? ""}`.trim()
  }
  const detail =
    tool.args.filename ?? tool.args.expression ?? tool.args.timezone
  const name =
    names[tool.toolName] ??
    tool.toolName.replace(/[_-]+/g, " ").replace(/^./, (c) => c.toUpperCase())
  return typeof detail === "string" ? `${name}: ${detail}` : name
}

/** Project the latest user turn, never infer a successful call from a stopped stream. */
export function activityForTurn(
  messages: readonly ThreadMessage[],
  running: boolean
) {
  const start = messages.findLastIndex((message) => message.role === "user")
  const turn = messages.slice(Math.max(0, start))
  const items: ActivityItem[] = []
  for (const message of turn) {
    if (message.role !== "assistant") continue
    for (const tool of message.content) {
      if (tool.type !== "tool-call") continue
      const result = readResult(tool.result)
      const settled = tool.result !== undefined && !tool.isPreliminary
      const approvalPending =
        tool.approval &&
        tool.approval.approved === undefined &&
        !tool.approval.resolution
      const waiting =
        !settled &&
        (approvalPending ||
          tool.interrupt ||
          message.status.type === "requires-action")
      const state: ActivityState =
        tool.isError || result.error
          ? "error"
          : settled
            ? "complete"
            : waiting
              ? "waiting"
              : running
                ? "running"
                : "stopped"
      items.push({ id: tool.toolCallId, label: toolLabel(tool), state, tool })
    }
  }
  const last = turn.findLast((message) => message.role === "assistant")
  const state: ActivityState =
    items.some((item) => item.state === "waiting") ||
    last?.status?.type === "requires-action"
      ? "waiting"
      : running
        ? "running"
        : last?.status?.type === "incomplete"
          ? last.status.reason === "error"
            ? "error"
            : "stopped"
          : items.some((item) => item.state === "error")
            ? "error"
            : items.some((item) => item.state === "stopped")
              ? "stopped"
              : "complete"
  return {
    items,
    state,
    lastMessageId: last?.id,
    hasMessages: messages.length > 0,
    hasPlanActivity: items.some((item) =>
      ["set_plan", "update_plan_step"].includes(item.tool.toolName)
    ),
    current: items.findLast(
      (item) => item.state === "running" || item.state === "waiting"
    ),
  }
}
