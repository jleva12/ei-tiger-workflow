import * as React from "react"

/*
 * The assistant's changes to a workflow or an agent, for the builder showing
 * it. The assistant's builder specialists save a draft on the server (the
 * workflow specialist's `edit_workflow`, forge-admin-api
 * `assistant/workflow_tools.py`; the agent specialist's `edit_agent` and
 * `write_instruction`, `assistant/agent_tools.py`); the builder open on that
 * workflow or agent loads the draft it saved, so the person sees the change
 * on the canvas at once, and can undo it there.
 *
 * The assistant reads its run's events as they stream in (`onEvent` in
 * `forge-agent.ts`) and announces each change here. The builders aren't
 * inside the assistant's runtime (they're pages of their own), and the
 * assistant may run in a window of its own, so changes go to this tab's
 * listeners and, over a BroadcastChannel, to the person's other tabs and
 * windows.
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

/** A change the assistant saved to an agent's draft (the Agents page's). */
export type AgentChange = {
  organizationId: string
  agentId: string
  /** The draft's revision once it saved. */
  revision: number
  /** What it did, in words, one line per change. */
  done: string[]
}

/** The tool whose answers say a workflow's draft changed. */
export const EDIT_TOOL = "edit_workflow"
/** The tools whose answers say an agent's draft changed. */
export const AGENT_EDIT_TOOLS: ReadonlySet<string> = new Set([
  "edit_agent",
  "write_instruction",
])

type Part = {
  functionResponse?: { name?: string; response?: unknown } | null
  function_response?: { name?: string; response?: unknown } | null
}

type Saved = {
  organizationId: string
  id: string
  revision: number
  done: string[]
}

/**
 * The drafts an event of the assistant's run says were saved: each answer
 * of one of `tools` in it that succeeded, with the record's ID in `idField`.
 */
function savedBy(
  event: { content?: { parts?: unknown[] | null } | null },
  tools: ReadonlySet<string>,
  idField: string
): Saved[] {
  const found: Saved[] = []
  for (const part of (event.content?.parts ?? []) as Part[]) {
    const response = part?.functionResponse ?? part?.function_response
    if (!response?.name || !tools.has(response.name)) continue
    const answer = response.response as Record<string, unknown> | undefined
    if (answer?.status !== "success") continue
    const payload = answer.payload as Record<string, unknown> | undefined
    const id = payload?.[idField]
    const { organization_id, revision, done } = payload ?? {}
    if (
      typeof id !== "string" ||
      typeof organization_id !== "string" ||
      typeof revision !== "number"
    )
      continue
    found.push({
      organizationId: organization_id,
      id,
      revision,
      done: Array.isArray(done)
        ? done.filter((line): line is string => typeof line === "string")
        : [],
    })
  }
  return found
}

/**
 * The workflow changes an event of the assistant's run carries: each
 * `edit_workflow` answer in it that succeeded. None for any other event.
 */
export function workflowChangesOf(event: {
  content?: { parts?: unknown[] | null } | null
}): WorkflowChange[] {
  return savedBy(event, new Set([EDIT_TOOL]), "workflow_id").map(
    ({ id, ...change }) => ({ ...change, workflowId: id })
  )
}

/**
 * The agent changes an event of the assistant's run carries: each
 * `edit_agent` or `write_instruction` answer in it that succeeded. None for
 * any other event.
 */
export function agentChangesOf(event: {
  content?: { parts?: unknown[] | null } | null
}): AgentChange[] {
  return savedBy(event, AGENT_EDIT_TOOLS, "agent_id").map(
    ({ id, ...change }) => ({ ...change, agentId: id })
  )
}

/**
 * What a change did, for the person: its first few lines, without the node
 * IDs the assistant names nodes by ("Look up the order (fetch_order)").
 */
export function changeSummary(change: { done: string[] }, most = 4) {
  return (
    change.done
      .slice(0, most)
      .map((line) => line.replace(/ \([a-z][a-z0-9_]{0,47}\)/g, ""))
      .join(". ") || undefined
  )
}

/**
 * Changes to one kind of record, told to the listeners in this tab and, over
 * `channelName`, in the person's other tabs and windows. `key` says which
 * record a change is to: its organization and ID.
 */
function changeBus<C>(
  channelName: string,
  key: (change: C) => readonly [string, string] | null
) {
  type Listener = (change: C) => void
  const listeners = new Set<Listener>()
  let channel: BroadcastChannel | null | undefined

  const tell = (change: C) => {
    for (const listener of [...listeners]) listener(change)
  }

  const shared = (): BroadcastChannel | null => {
    if (channel !== undefined) return channel
    channel =
      typeof BroadcastChannel === "undefined"
        ? null
        : new BroadcastChannel(channelName)
    channel?.addEventListener("message", (message: MessageEvent) => {
      const change = message.data as C | undefined
      if (change && key(change)) tell(change)
    })
    // Node's (the tests') would keep its process going; browsers have no unref.
    ;(channel as { unref?: () => void } | null)?.unref?.()
    return channel
  }

  /** Tells every builder, in this tab and the person's others, of a change. */
  const announce = (change: C) => {
    tell(change)
    shared()?.postMessage(change)
  }

  /**
   * Calls `onChange` for each change the assistant saves to this record,
   * while the component is mounted.
   */
  const useChanges = (
    organizationId: string,
    id: string,
    onChange: (change: C) => void
  ) => {
    const latest = React.useRef(onChange)
    React.useEffect(() => {
      latest.current = onChange
    })
    React.useEffect(() => {
      shared()
      const listener: Listener = (change) => {
        const [organization, record] = key(change) ?? []
        if (organization === organizationId && record === id)
          latest.current(change)
      }
      listeners.add(listener)
      return () => {
        listeners.delete(listener)
      }
    }, [organizationId, id])
  }

  return { announce, useChanges }
}

const textual = (a: unknown, b: unknown) =>
  typeof a === "string" && typeof b === "string" ? ([a, b] as const) : null

const workflows = changeBus<WorkflowChange>("forge-workflow-changes", (c) =>
  textual(c.organizationId, c.workflowId)
)
const agents = changeBus<AgentChange>("forge-agent-changes", (c) =>
  textual(c.organizationId, c.agentId)
)

/** Tells every workflow builder, in this tab and the person's others, of a change. */
export const announceWorkflowChange = workflows.announce
/** Tells every agent builder, in this tab and the person's others, of a change. */
export const announceAgentChange = agents.announce

/**
 * Calls `onChange` for each change the assistant saves to this workflow,
 * while the component is mounted.
 */
export const useWorkflowChanges = workflows.useChanges

/**
 * Calls `onChange` for each change the assistant saves to this agent, while
 * the component is mounted.
 */
export const useAgentChanges = agents.useChanges
