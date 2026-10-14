import {
  kindCatalog,
  outputsOfKind,
  type Catalog,
  type CatalogKind,
} from "@/features/builder/lib/catalog"
import type {
  KindGroup,
  KindInfo,
  StepOutput,
} from "@/features/builder/lib/types"
// Types only: the model reads its kinds from here.
import type { ChatConfigs, ChatKind } from "./model"

/*
 * The kinds of node a chat agent can have on the Agents canvas, as the
 * admin API serves them (`GET /chat-agent-node-kinds`, the agent runtime's
 * node catalog, packages/python/agent-runtime's node_kinds.json): each
 * kind's words, its library group, its ways out (an agent's tools and who
 * it hands off to), whether it can be handed off to, whether a workflow's
 * LLM agent may have it as a tool, and what a new one starts with
 * (builder/lib/catalog). The canvas and a workflow's LLM agents' tools read
 * them from here once it's installed (NodeCatalogGate).
 */

/** One kind of node on the Agents canvas, as served. */
export type ChatCatalogKind = CatalogKind & {
  /** Whether an agent can hand off to it. */
  handed_to?: boolean
  /** Whether a workflow's LLM agent may have it as a tool. */
  workflow_tool?: boolean
}

/** What `GET /chat-agent-node-kinds` answers. */
export type ChatNodeCatalog = Catalog<ChatCatalogKind>

/** A kind as the library, the canvas and the settings show it. */
export type ChatKindInfo<K extends ChatKind = ChatKind> = KindInfo & {
  defaults: () => ChatConfigs[K]
}

/**
 * The kinds the Agents canvas can set up: its settings and checks are
 * written for these. A served kind that isn't one is left out.
 */
const SET_UP: Record<ChatKind, true> = {
  agent: true,
  sub_agent: true,
  saved_agent: true,
  memory: true,
  knowledge_base: true,
  http_tool: true,
  openapi: true,
  mcp: true,
  adk_workflow: true,
  code_execution: true,
  skills: true,
}

const catalog = kindCatalog<ChatKind, ChatCatalogKind>(
  "chat agent node catalog",
  SET_UP
)

/** Every kind the canvas can set up, served or not: the format's. */
export const KNOWN_CHAT_KINDS = catalog.known

/** Makes `served` the one every kind is read from. */
export const installChatNodeCatalog = (served: ChatNodeCatalog) =>
  catalog.install(served)

export const chatNodeCatalogInstalled = catalog.installed

/** Whether a kind is served and the canvas can set it up. */
export const isServedChatKind = catalog.isServed

/** A served kind, as the library, the canvas and the settings show it. */
export const chatKindInfo = <K extends ChatKind>(kind: K) =>
  catalog.info(kind) as unknown as ChatKindInfo<K>

/** Every served kind the canvas can set up, in the library's order. */
export const servedChatKinds = catalog.list

/** The same, by kind: what the builder kit reads. */
export const servedChatKindInfos = catalog.infos

/** The library's groups, in order. */
export const chatKindGroups = (): KindGroup[] => catalog.groups()

/** A node's ways out: an agent's tools and who it hands off to; none for the rest. */
export const chatWaysOut = (kind: ChatKind): StepOutput[] =>
  outputsOfKind(catalog.entry(kind))

/** Whether a kind is an agent: one with the two ways out. */
export const isAgentLike = (kind: ChatKind) =>
  catalog.entry(kind).outputs.length > 0

/** Whether an agent can hand off to a kind. */
export const isHandedTo = (kind: ChatKind) =>
  Boolean(catalog.entry(kind).handed_to)

/** Whether a workflow's LLM agent may have a kind as a tool. */
export const isWorkflowTool = (kind: string) =>
  catalog.isServed(kind) && Boolean(catalog.entry(kind).workflow_tool)
