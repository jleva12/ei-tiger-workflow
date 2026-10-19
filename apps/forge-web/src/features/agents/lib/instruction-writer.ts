import { useMutation } from "@tanstack/react-query"

import type {
  TemplateAI,
  TemplateAIRequest,
} from "@/features/builder/components/fields/markdown-template-editor"
import { callHelper } from "@/features/helpers/lib/api"
import type { SpecSummary } from "@/features/credentials/lib/api"
import type { Scope } from "@/features/steps/lib/scope"
import { typeLabel, type DataType } from "@/features/steps/lib/types"
import type { ChatConfigs, ChatKind } from "./model"

/*
 * The instruction writer: a helper agent of the admin API's
 * (`instruction_writer` in helpers.yaml) that writes an LLM agent's
 * instruction from what the person says it should do. It's sent a
 * brief of the agent being set up, as the builder holds it (saved or not):
 * where it runs, its tools, who it hands off to, and the run's data its
 * instruction can put in with `{{ }}`. The admin API fills in what the
 * builder only has IDs for (an MCP server's tools, a knowledge base's
 * description, another agent's or workflow's), and answers with the
 * instruction and a few notes on what the agent's still missing.
 *
 * Tools go with only what says what they do: never an HTTP tool's headers
 * or body, an MCP server's headers or a credential.
 */

/** One of the agent's tools: its kind, its name, and what it's set up to do. */
export type BriefTool = {
  kind: ChatKind
  name: string
  config: Record<string, unknown>
}

/** An agent it hands off to, or that hands off to it. */
export type BriefAgent = {
  /** A workflow's sub-agent's ID: its answer is kept as state.<id>. */
  id?: string
  name: string
  description: string
  /** sub_agent, saved_agent (Agents page); llm, sequential, parallel, loop_agent (a workflow's). */
  kind: string
  /** How it's handed to: chat, task or single_turn; tool when it's called as one. */
  mode?: string
  /** An agent from the Agents page, by ID. */
  agent?: string
}

/** A value the instruction can put in with `{{ }}`. */
export type BriefReference = {
  path: string
  type: string
  description?: string
}

export type InstructionBrief = {
  /** A chat on the Agents page, or a step of a workflow. */
  where: "chat" | "workflow"
  /** The chat agent people talk to, one of its sub-agents, a workflow's LLM node or a sub-agent of one. */
  role: "chat_agent" | "sub_agent" | "workflow_agent" | "workflow_sub_agent"
  name: string
  description: string
  /** The Agents page's agent (or the workflow) it's part of. */
  container: string
  /** A sub-agent's hand-off mode; a workflow node's single_turn or task. */
  mode?: string
  ask_person: boolean
  /** It sees only what it's given, not the conversation so far. */
  sees_only_given: boolean
  /** The JSON Schema its answer is held to; none: free text. */
  output_schema?: Record<string, unknown>
  /** It's given tools that load and save the run's files. */
  files: boolean
  tools: BriefTool[]
  hand_offs: BriefAgent[]
  /** The agent that hands off to it, for a sub-agent. */
  parent?: BriefAgent
  references: BriefReference[]
}

export type InstructionDraft = {
  instruction: string
  /** What the person might add or change: a tool, a state field… */
  notes: string[]
  /** The model that wrote it (`provider/model`). */
  model: string
}

export type InstructionRequest = {
  /** What the person asked for: what the agent should do, or what to change. */
  ask: string
  /** The instruction to start from: the field's, or the last draft; empty for none. */
  current: string
  brief: InstructionBrief
}

export function useWriteInstruction(organizationId: string) {
  return useMutation({
    mutationFn: async ({
      request,
      signal,
    }: {
      request: InstructionRequest
      signal?: AbortSignal
    }): Promise<InstructionDraft> => {
      const answer = await callHelper<
        InstructionRequest,
        { instruction: string }
      >(organizationId, "instruction_writer", request, { signal })
      return {
        instruction: answer.result.instruction,
        notes: answer.notes,
        model: answer.model,
      }
    },
  })
}

/* -------------------------------------------------------------------------- */
/* The editor's AI                                                            */
/* -------------------------------------------------------------------------- */

/**
 * The AI inside an instruction's rich text editor: the admin API's
 * `instruction_editor` helper rewrites the selected passage, or writes what
 * goes in at the caret, as asked, knowing the agent from its brief (when
 * there is one) so it names the agent's real tools and values.
 */
export function instructionEditor(
  organizationId: string,
  brief?: () => Promise<InstructionBrief> | InstructionBrief
): TemplateAI {
  return async (request, signal) => {
    const answer = await callHelper<
      TemplateAIRequest & { brief?: InstructionBrief },
      { markdown: string }
    >(
      organizationId,
      "instruction_editor",
      { ...request, ...(brief && { brief: await brief() }) },
      { signal }
    )
    return answer.result.markdown
  }
}

/* -------------------------------------------------------------------------- */
/* Tools                                                                      */
/* -------------------------------------------------------------------------- */

const pick = <T extends object, K extends keyof T>(
  config: T,
  keys: readonly K[]
): Record<string, unknown> =>
  Object.fromEntries(keys.map((key) => [key, config[key]]))

/** What a tool's settings say about what it does, and nothing that signs it in. */
export function briefTool<K extends ChatKind>(
  kind: K,
  name: string,
  config: ChatConfigs[K],
  operations?: SpecSummary["operations"]
): BriefTool {
  const c = config as ChatConfigs[keyof ChatConfigs]
  switch (kind) {
    case "http_tool": {
      const http = c as ChatConfigs["http_tool"]
      return {
        kind,
        name,
        config: {
          ...pick(http, ["description", "method", "parameters", "confirm"]),
          // Where it calls, without a query string that might hold a key.
          url: http.url.split("?")[0],
        },
      }
    }
    case "mcp": {
      const mcp = c as ChatConfigs["mcp"]
      return {
        kind,
        name,
        config: {
          server: mcp.server,
          url: mcp.server ? "" : mcp.url.split("?")[0],
          tools: mcp.tools,
          confirm: mcp.confirm,
        },
      }
    }
    case "openapi": {
      const spec = c as ChatConfigs["openapi"]
      const wanted = spec.operations
        .split(",")
        .map((o) => o.trim())
        .filter(Boolean)
      return {
        kind,
        name,
        config: {
          url: spec.source === "url" ? spec.url.split("?")[0] : "",
          operations: (operations ?? [])
            .filter(
              (o) =>
                !wanted.length ||
                wanted.includes(o.name) ||
                wanted.includes(o.operation_id)
            )
            .slice(0, 60)
            .map((o) => ({
              name: o.name,
              method: o.method,
              path: o.path,
              summary: o.summary || o.description.slice(0, 200),
            })),
          only: wanted,
          confirm: spec.confirm,
        },
      }
    }
    case "sub_agent":
      return {
        kind,
        name,
        config: pick(c as ChatConfigs["sub_agent"], ["description"]),
      }
    case "intake_agent": {
      // Used as a tool: the agent asks its fields itself.
      const intake = c as ChatConfigs["intake_agent"]
      return {
        kind,
        name,
        config: {
          description: intake.description,
          fields: intake.fields.map((f) =>
            pick(f, ["key", "label", "type", "options", "required"])
          ),
        },
      }
    }
    default:
      return { kind, name, config: { ...c } }
  }
}

/* -------------------------------------------------------------------------- */
/* References                                                                 */
/* -------------------------------------------------------------------------- */

const MOST_REFERENCES = 80
// How deep a root's fields are listed: request.user.name, input.order.id.
const DEPTH = 2

/** What the instruction can put in, as the field completes it: each root's fields and each earlier step's output. */
export function scopeReferences(scope: Scope): BriefReference[] {
  const found: BriefReference[] = []
  const add = (path: string, type: DataType, description?: string) => {
    if (found.length >= MOST_REFERENCES) return
    found.push({
      path,
      type: typeLabel(type),
      ...((description ?? type.description)
        ? { description: description ?? type.description }
        : {}),
    })
    if (type.kind !== "object" || path.split(".").length > DEPTH) return
    for (const [field, inner] of Object.entries(type.properties)) {
      add(`${path}.${field}`, inner)
    }
  }
  for (const root of scope.roots.values()) {
    // The steps are listed one by one below.
    if (root.name === "steps") continue
    add(root.name, root.type, root.detail)
  }
  for (const step of scope.steps.values()) {
    add(`steps.${step.id}.output`, step.output, `What ${step.name} handed on.`)
  }
  return found
}
