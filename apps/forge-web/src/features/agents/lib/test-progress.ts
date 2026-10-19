import * as React from "react"
import type { AssistantRuntime, ThreadMessage } from "@assistant-ui/react"
import type { AdkEvent } from "@assistant-ui/react-google-adk"

import { adkName } from "@/features/adk-workflows/lib/model"
import type {
  RunOverlay,
  StepRunState,
} from "@/features/builder/components/run-overlay"
import type { ChatAgentDocument, ChatAgentNode } from "./document"
import { TOOLS } from "./model"

/*
 * A test chat drawn over the agent's canvas, as the workflow builder draws a
 * test run: the agent answering (and each tool it's waiting on) spinning,
 * what it went through this turn ticked, and the lines along the way. The
 * runtime's events say which agent sent them (`author`) and what it calls
 * (`functionCall`), by the names ADK gives them, which the runtime makes from
 * the nodes' names (forge_agent_runtime's build), so they lead back to the
 * canvas.
 */

type Doc = Pick<ChatAgentDocument, "nodes" | "edges">

/** ADK's call that hands the conversation to another agent. */
const TRANSFER = "transfer_to_agent"
/** ADK's call asking the person to confirm another call. */
const CONFIRMATION = "adk_request_confirmation"
const MEMORY_TOOLS = new Set(["load_memory", "preload_memory"])
/** ADK's SkillToolset: one for all of an agent's Skills nodes. */
const SKILL_TOOLS = new Set([
  "list_skills",
  "search_skills",
  "load_skill",
  "unload_skill",
  "load_skill_resource",
  "run_skill_script",
])
/**
 * The tools an agent files requests to its intake agents with (tool mode):
 * the first names the intakes, the others the requests it started.
 */
const START_REQUESTS = "start_requests"
const REQUEST_TOOLS = new Set([
  "start_requests",
  "answer_requests",
  "review_requests",
  "submit_requests",
  "cancel_requests",
  "list_requests",
])
/** Rich answers' one tool. */
const PRESENT = "present"
/** The tool reading a knowledge base tool's system designs, after its name. */
const DESIGN_SUFFIX = "_system_design"
/** The longest tool name model APIs take. */
const MAX_TOOL_NAME = 64
/** How deep saved agents using saved agents are followed. */
const MAX_SAVED_DEPTH = 4

/** What the canvas alone doesn't say: what the agent's saved agents and MCP servers hold. */
export type TestGraphLookups = {
  /** A saved agent's document, by its ID, once loaded. */
  savedAgent?: (id: string) => Doc | undefined
  /** One of the organization's MCP servers' tools, once checked. */
  mcpTools?: (server: string) => readonly string[] | null | undefined
}

/** The canvas as a test chat's events reach it. */
export type TestGraph = {
  /** Each agent's node, by the name ADK gives it; a saved agent's agents are all its one node. */
  agents: ReadonlyMap<string, string>
  /** The agent a chat starts with. */
  entry: string | undefined
  /** The nodes a call reaches: what the agent (a node) calls by that name. */
  callees: (
    agent: string,
    call: string,
    args: Record<string, unknown>
  ) => string[]
  /** The line from one node to another, by its ID. */
  edge: (source: string, target: string) => string | undefined
  /** One of the intake agents an agent files requests to, by the name its requests give. */
  intake: (agent: string, title: string) => string | undefined
}

const listed = (value: unknown) =>
  String(value ?? "")
    .split(",")
    .map((name) => name.trim())
    .filter(Boolean)

/** An operation ID as ADK names its tool: `getInvoice`: `get_invoice`. */
const snake = (text: string) =>
  text
    .replace(/[^A-Za-z0-9]+/g, "_")
    .replace(/([A-Z]+)([A-Z][a-z])/g, "$1_$2")
    .replace(/([a-z0-9])([A-Z])/g, "$1_$2")
    .toLowerCase()
    .replace(/^_+|_+$/g, "")

/** Its agents by their ADK names; a saved agent's, as far as they're known, are its node. */
function agentsOf(
  doc: Doc,
  lookups: TestGraphLookups,
  seen: readonly string[] = []
): Map<string, string> {
  const agents = new Map<string, string>()
  for (const node of doc.nodes) {
    if (
      node.kind === "agent" ||
      node.kind === "sub_agent" ||
      node.kind === "intake_agent" ||
      // A saved agent is called after the node it's placed at.
      node.kind === "saved_agent"
    ) {
      const name = adkName(node.name)
      if (name && !agents.has(name)) agents.set(name, node.id)
    }
  }
  for (const node of doc.nodes) {
    if (node.kind !== "saved_agent") continue
    const id = node.config.agent
    if (!id || seen.includes(id) || seen.length >= MAX_SAVED_DEPTH) continue
    const saved = lookups.savedAgent?.(id)
    if (!saved) continue
    for (const name of agentsOf(saved, lookups, [...seen, id]).keys()) {
      if (!agents.has(name)) agents.set(name, node.id)
    }
  }
  return agents
}

/**
 * Whether a tool node offers the tool called by that name: "maybe" for a
 * toolset whose tools aren't known here (an MCP server set up on the node,
 * an OpenAPI spec without a list of operations).
 */
function offers(
  node: ChatAgentNode,
  call: string,
  lookups: TestGraphLookups
): boolean | "maybe" {
  const name = adkName(node.name)
  switch (node.kind) {
    case "http_tool":
      return name === call
    case "code_execution":
      return (name || "run_code") === call
    case "knowledge_base": {
      const search = name || "search_knowledge_base"
      const design = `${search.slice(0, MAX_TOOL_NAME - DESIGN_SUFFIX.length)}${DESIGN_SUFFIX}`
      return call === search || call === design
    }
    case "adk_workflow":
      // Without a name of its own, it's called after its workflow.
      return name ? name === call : "maybe"
    case "memory":
      return MEMORY_TOOLS.has(call)
    case "rich_answers":
      return call === PRESENT
    case "skills":
      return SKILL_TOOLS.has(call)
    case "mcp": {
      const allowed = listed(node.config.tools)
      if (allowed.length) return allowed.includes(call)
      const tools = node.config.server
        ? lookups.mcpTools?.(node.config.server)
        : undefined
      return tools ? tools.includes(call) : "maybe"
    }
    case "openapi": {
      const allowed = listed(node.config.operations)
      return allowed.length
        ? allowed.some((op) => op === call || snake(op) === call)
        : "maybe"
    }
    default:
      return false
  }
}

/** The canvas's agents, tools and lines, as a test chat's events name them. */
export function testGraphOf(
  doc: Doc,
  lookups: TestGraphLookups = {}
): TestGraph {
  const agents = agentsOf(doc, lookups)
  const byId = new Map(doc.nodes.map((node) => [node.id, node]))
  const lines = new Map(
    doc.edges.map((edge) => [`${edge.source}\u0000${edge.target}`, edge.id])
  )
  const toolsOf = (agent: string) =>
    doc.edges.flatMap((edge) => {
      const node =
        edge.source === agent && edge.source_output === TOOLS
          ? byId.get(edge.target)
          : undefined
      return node ? [node] : []
    })
  return {
    agents,
    entry: doc.nodes.find((node) => node.kind === "agent")?.id,
    callees(agent, call, args) {
      if (call === TRANSFER) {
        const to = agents.get(String(args.agent_name ?? ""))
        return to ? [to] : []
      }
      // An agent it asks (a tool) or hands a task to: called by its name.
      const asked = agents.get(call)
      if (asked) return [asked]
      const tools = toolsOf(agent)
      if (REQUEST_TOOLS.has(call)) {
        // The intakes it names; requests name theirs in their answers.
        const intakes = tools.filter((node) => node.kind === "intake_agent")
        const named = new Set(
          Array.isArray(args.intakes) ? args.intakes.map(String) : []
        )
        const picked = intakes.filter((node) => named.has(adkName(node.name)))
        if (call === START_REQUESTS && picked.length) {
          return picked.map((node) => node.id)
        }
        return intakes.length === 1 ? [intakes[0].id] : []
      }
      const sure = tools.filter((node) => offers(node, call, lookups) === true)
      if (sure.length) return sure.map((node) => node.id)
      // Only one of its toolsets could have it: that one.
      const maybe = tools.filter(
        (node) => offers(node, call, lookups) === "maybe"
      )
      return maybe.length === 1 ? [maybe[0].id] : []
    },
    edge: (source, target) => lines.get(`${source}\u0000${target}`),
    intake: (agent, title) =>
      toolsOf(agent).find(
        (node) => node.kind === "intake_agent" && node.name === title
      )?.id,
  }
}

type OpenCall = {
  /** The agent that made it, its node. */
  agent: string
  /** What it reached. */
  nodes: string[]
  /** Left open for the person: a form, a confirmation, a task talking with them. */
  waits: boolean
}

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value)

/** A tool's answer saying it failed: ADK's and Forge's tools answer `{ error }`. */
const failedAnswer = (response: unknown) =>
  isRecord(response) && Boolean(response.error)

/** A test chat's turns, followed over the canvas. */
export type TestProgress = {
  /** A turn starts: the last turn's marks go, the agent answering works. */
  start: () => void
  /** One of the turn's events came in; true when the drawing changed. */
  event: (event: AdkEvent) => boolean
  /** The turn is over: it stopped with an error when `failed`. */
  end: (failed?: boolean) => void
  /** Another conversation: nothing of this one is drawn. */
  reset: () => void
  /** What the canvas draws now. */
  overlay: () => RunOverlay
  /** The canvas changed: its events lead to these nodes now. */
  use: (graph: TestGraph) => void
}

export function createTestProgress(graph: TestGraph): TestProgress {
  let steps = new Map<string, StepRunState>()
  let taken = new Set<string>()
  let live = false
  // The agent answering now, its node: the next turn starts with it too,
  // as ADK carries on with the agent the conversation was handed to.
  let speaking: string | undefined
  // Calls not answered yet, by ID.
  const calls = new Map<string, OpenCall>()
  // The intake agent each request filed in this conversation is with.
  const requests = new Map<string, string>()
  let changed = false

  const set = (node: string, state: StepRunState) => {
    if (steps.get(node) === state) return
    steps.set(node, state)
    changed = true
  }
  const take = (source: string, target: string) => {
    const id = graph.edge(source, target)
    if (!id || taken.has(id)) return
    taken.add(id)
    changed = true
  }
  const waitedOn = (node: string) =>
    [...calls.values()].some((call) => call.nodes.includes(node))
  // The requests a request tool answers about (`{ id, intake }`, in its
  // lists), kept with their intakes; those intakes' nodes.
  const filedWith = (response: unknown, agent: string) => {
    const nodes = new Set<string>()
    if (!isRecord(response)) return nodes
    for (const value of Object.values(response)) {
      if (!Array.isArray(value)) continue
      for (const item of value) {
        if (!isRecord(item) || typeof item.intake !== "string") continue
        const node = graph.intake(agent, item.intake)
        if (!node) continue
        nodes.add(node)
        if (typeof item.id === "string") requests.set(item.id, node)
      }
    }
    return nodes
  }
  const speak = (node: string) => {
    if (node !== speaking) {
      if (speaking) {
        if (steps.get(speaking) === "running") set(speaking, "done")
        // Handed on along its line.
        take(speaking, node)
      }
      speaking = node
    }
    set(node, "running")
  }

  return {
    start() {
      steps = new Map()
      taken = new Set()
      live = true
      // The person's message answers what asked them (a form, a
      // confirmation); a task left talking with them goes on: it was handed
      // to along its line, and the agent that handed it waits for it.
      const agents = new Set(graph.agents.values())
      for (const [id, call] of calls) {
        if (!call.nodes.some((node) => agents.has(node))) {
          calls.delete(id)
          continue
        }
        for (const node of call.nodes) take(call.agent, node)
        set(call.agent, "done")
      }
      speaking ??= graph.entry
      if (speaking) set(speaking, "running")
      changed = false
    },
    event(event) {
      changed = false
      const known = graph
      const author =
        event.author && event.author !== "user"
          ? known.agents.get(event.author)
          : undefined
      if (author) speak(author)
      const from = author ?? speaking
      for (const part of event.content?.parts ?? []) {
        const call = part.functionCall
        if (call && from) {
          const args = (call.args ?? {}) as Record<string, unknown>
          // A confirmation waits on the call it confirms.
          const confirming =
            call.name === CONFIRMATION && isRecord(args.originalFunctionCall)
              ? calls.get(String(args.originalFunctionCall.id ?? ""))
              : undefined
          let nodes = confirming
            ? confirming.nodes
            : known.callees(from, call.name, args)
          // Requests it filed earlier: with their intakes.
          if (!nodes.length && Array.isArray(args.requests)) {
            nodes = [
              ...new Set(
                args.requests.flatMap((id) => {
                  const intake = requests.get(String(id))
                  return intake ? [intake] : []
                })
              ),
            ]
          }
          for (const node of nodes) {
            take(from, node)
            // A hand-off's agent works once it answers.
            if (call.name !== TRANSFER) set(node, "running")
          }
          if (call.id && call.name !== TRANSFER) {
            calls.set(call.id, {
              agent: from,
              nodes,
              waits: event.longRunningToolIds?.includes(call.id) ?? false,
            })
          }
        }
        const answer = part.functionResponse
        if (answer && from) {
          const open = answer.id ? calls.get(answer.id) : undefined
          if (open && answer.id) calls.delete(answer.id)
          // One made last turn (a call the person confirmed): by its name.
          const nodes = open ? open.nodes : known.callees(from, answer.name, {})
          if (!open) for (const node of nodes) take(from, node)
          const filed = filedWith(answer.response, from)
          for (const node of filed) if (!nodes.includes(node)) take(from, node)
          for (const node of new Set([...nodes, ...filed])) {
            if (waitedOn(node) || node === speaking) continue
            set(node, failedAnswer(answer.response) ? "failed" : "done")
          }
        }
      }
      const handedTo = event.actions?.transferToAgent
      const to = handedTo ? known.agents.get(handedTo) : undefined
      if (to && from) take(from, to)
      if (event.errorCode && from) set(from, "failed")
      return changed
    },
    end(failed = false) {
      live = false
      for (const [id, call] of calls) {
        if (!call.waits) {
          calls.delete(id)
          continue
        }
        // Waiting for the person: on the tool or task, or the agent asking.
        for (const node of call.nodes.length ? call.nodes : [call.agent]) {
          set(node, "waiting")
        }
      }
      for (const [node, state] of steps) {
        if (state !== "running") continue
        set(node, failed && node === speaking ? "failed" : "done")
      }
    },
    reset() {
      steps = new Map()
      taken = new Set()
      live = false
      speaking = undefined
      calls.clear()
      requests.clear()
    },
    overlay: () => ({ steps: new Map(steps), taken: new Set(taken), live }),
    use(next) {
      graph = next
    },
  }
}

/** The last reply stopped with an error. */
const endedInError = (messages: readonly ThreadMessage[]) => {
  const last = messages.at(-1)
  return (
    last?.role === "assistant" &&
    last.status.type === "incomplete" &&
    last.status.reason === "error"
  )
}

/**
 * A test chat followed over the canvas: pass `onEvent` to the chat's
 * `useAdkAssistant` and `follow` its runtime, for its turns. What to draw
 * goes to `onChange` as it changes, and null once the chat closes or moves
 * to another conversation.
 */
export function useTestProgress(
  graph: TestGraph,
  onChange: (overlay: RunOverlay | null) => void
) {
  const onChangeRef = React.useRef(onChange)
  React.useEffect(() => {
    onChangeRef.current = onChange
  })
  const [progress] = React.useState(() => createTestProgress(graph))
  React.useEffect(() => progress.use(graph), [progress, graph])
  const onEvent = React.useCallback(
    (event: AdkEvent) => {
      if (progress.event(event)) onChangeRef.current(progress.overlay())
    },
    [progress]
  )
  const follow = React.useCallback(
    (runtime: AssistantRuntime) => {
      let running = runtime.thread.getState().isRunning
      let thread = runtime.threads.getState().mainThreadId
      const onThread = () => {
        const main = runtime.threads.getState().mainThreadId
        if (main === thread) return
        thread = main
        running = runtime.thread.getState().isRunning
        progress.reset()
        onChangeRef.current(null)
      }
      const onTurn = () => {
        const state = runtime.thread.getState()
        if (state.isRunning === running) return
        running = state.isRunning
        if (running) progress.start()
        else progress.end(endedInError(state.messages))
        onChangeRef.current(progress.overlay())
      }
      const stops = [
        runtime.threads.subscribe(onThread),
        runtime.thread.subscribe(onTurn),
      ]
      return () => stops.forEach((stop) => stop())
    },
    [progress]
  )
  React.useEffect(() => () => onChangeRef.current(null), [])
  return { onEvent, follow }
}
