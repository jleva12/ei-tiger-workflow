import * as React from "react"

/*
 * The assistant's changes to a workflow, for the builder showing it. The
 * assistant's workflow specialist saves a workflow's draft on the server
 * (its `edit_workflow` tool, forge-admin-api `assistant/workflow_tools.py`);
 * the builder open on that workflow loads the draft it saved, so the person
 * sees the change on the canvas at once, and can undo it there.
 *
 * The assistant reads its run's events as they stream in (`onEvent` in
 * `forge-agent.ts`) and announces each change here. The builder isn't inside
 * the assistant's runtime (it's a page of its own), and the assistant may run
 * in a window of its own, so changes go to this tab's listeners and, over a
 * BroadcastChannel, to the person's other tabs and windows.
 */

/** A change the assistant saved to a workflow's draft. */
export type WorkflowChange = {
  organizationId: string
  workflowId: string
  /** The draft's revision once it saved. */
  revision: number
  /** What it did, in words, one line per change. */
  done: string[]
}

/** The tool whose answers say a workflow's draft changed. */
export const EDIT_TOOL = "edit_workflow"

type Part = {
  functionResponse?: { name?: string; response?: unknown } | null
  function_response?: { name?: string; response?: unknown } | null
}

/**
 * The workflow changes an event of the assistant's run carries: each
 * `edit_workflow` answer in it that succeeded. None for any other event.
 */
export function workflowChangesOf(event: {
  content?: { parts?: unknown[] | null } | null
}): WorkflowChange[] {
  const found: WorkflowChange[] = []
  for (const part of (event.content?.parts ?? []) as Part[]) {
    const response = part?.functionResponse ?? part?.function_response
    if (!response || response.name !== EDIT_TOOL) continue
    const answer = response.response as Record<string, unknown> | undefined
    if (answer?.status !== "success") continue
    const payload = answer.payload as Record<string, unknown> | undefined
    const { workflow_id, organization_id, revision, done } = payload ?? {}
    if (
      typeof workflow_id !== "string" ||
      typeof organization_id !== "string" ||
      typeof revision !== "number"
    )
      continue
    found.push({
      organizationId: organization_id,
      workflowId: workflow_id,
      revision,
      done: Array.isArray(done)
        ? done.filter((line): line is string => typeof line === "string")
        : [],
    })
  }
  return found
}

type Listener = (change: WorkflowChange) => void

const listeners = new Set<Listener>()
const CHANNEL = "forge-workflow-changes"
let channel: BroadcastChannel | null | undefined

function shared(): BroadcastChannel | null {
  if (channel !== undefined) return channel
  channel =
    typeof BroadcastChannel === "undefined"
      ? null
      : new BroadcastChannel(CHANNEL)
  channel?.addEventListener("message", (message: MessageEvent) => {
    const change = message.data as WorkflowChange | undefined
    if (change && typeof change.workflowId === "string") tell(change)
  })
  // Node's (the tests') would keep its process going; browsers have no unref.
  ;(channel as { unref?: () => void } | null)?.unref?.()
  return channel
}

function tell(change: WorkflowChange) {
  for (const listener of [...listeners]) listener(change)
}

/** Tells every builder, in this tab and the person's others, of a change. */
export function announceWorkflowChange(change: WorkflowChange) {
  tell(change)
  shared()?.postMessage(change)
}

/**
 * Calls `onChange` for each change the assistant saves to this workflow,
 * while the component is mounted.
 */
export function useWorkflowChanges(
  organizationId: string,
  workflowId: string,
  onChange: (change: WorkflowChange) => void
) {
  const latest = React.useRef(onChange)
  React.useEffect(() => {
    latest.current = onChange
  })
  React.useEffect(() => {
    shared()
    const listener: Listener = (change) => {
      if (
        change.organizationId === organizationId &&
        change.workflowId === workflowId
      )
        latest.current(change)
    }
    listeners.add(listener)
    return () => {
      listeners.delete(listener)
    }
  }, [organizationId, workflowId])
}
