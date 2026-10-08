import type { BuilderUi, NodeSummary } from "@/features/builder/components/ui"
import {
  AGENTS_ICON,
  isForgeKind,
  subAgentsOf,
  type AgentStep,
  type SubAgent,
} from "@/features/adk-workflows/lib/model"
import { describeTypes } from "@/features/adk-workflows/lib/files"
import type { StepData } from "@/features/steps/lib/model"
import { inputFields } from "@/features/steps/lib/scope"
import { AgentDataSection } from "./agent-data-section"
import { AgentFields } from "./agent-fields"
import { modelLine, plural, subAgentLine } from "./agent-lines"
import type { AgentLookups } from "./agent-store"
import { stepDetail, stepSummary } from "./step-lines"

/*
 * What the agent builder shows that the builder kit can't know: its words,
 * each kind's settings (./agent-fields), the line under a node's name and
 * on its card, and how a node's data flows (./agent-data-section). Forge's
 * steps show what they do in a workflow.
 */

/** What a node shows under its name: one fact about how it's set up. */
function detailOf(step: AgentStep, lookups: AgentLookups): string {
  if (isForgeKind(step.kind)) return stepDetail(step as StepData)
  switch (step.kind) {
    case "start": {
      const { names } = inputFields(step.config.input_schema)
      const input = names.length
        ? `Needs ${plural(names.length, "input field")}`
        : "Takes any input"
      return step.config.allow_files
        ? `${input} · files: ${describeTypes(step.config.file_types)}`
        : input
    }
    case "llm": {
      const config = step.config
      if (config.source === "agent") {
        if (!config.agent) return "An agent from the Agents page: none picked"
        const name = lookups.chatAgents[config.agent]?.name ?? "A deleted agent"
        const version =
          config.version === null
            ? "latest"
            : config.version === "draft"
              ? "its draft"
              : `v${config.version}`
        return `${name} · ${version}`
      }
      const tools = config.tools.length
      const handsOff = config.sub_agents.length
      return [
        modelLine(config, lookups),
        tools && plural(tools, "tool"),
        handsOff && `hands off to ${handsOff}`,
      ]
        .filter(Boolean)
        .join(" · ")
    }
    case "sequential":
    case "parallel":
    case "loop_agent":
      return subAgentLine(
        {
          id: "",
          kind: step.kind,
          name: step.name,
          config: step.config,
        } as SubAgent,
        lookups
      )
    case "saved": {
      const name = lookups.agents[step.config.agent]
      return name ? `Runs ${name}` : "No workflow picked"
    }
    case "human_input": {
      const { names } = inputFields(step.config.response_schema)
      return names.length
        ? `Asks for ${plural(names.length, "field")}`
        : "Asks a person"
    }
  }
  return ""
}

/** A node's line on its card: what it does. */
function summaryOf(step: AgentStep): NodeSummary | null {
  if (isForgeKind(step.kind)) return stepSummary(step as StepData)
  switch (step.kind) {
    case "start": {
      const { names, required } = inputFields(step.config.input_schema)
      return names.length
        ? {
            code: names.map((n) => (required.has(n) ? n : `${n}?`)).join(" · "),
          }
        : null
    }
    case "llm":
      return step.config.instruction.trim()
        ? { text: step.config.instruction.trim() }
        : null
    case "sequential":
    case "loop_agent":
    case "parallel": {
      const names = subAgentsOf(step).map((a) => a.name.trim() || "Unnamed")
      if (!names.length) return { text: "No sub-agents yet" }
      return { text: names.join(step.kind === "parallel" ? " · " : " → ") }
    }
    case "human_input":
      return step.config.message.trim()
        ? { text: step.config.message.trim() }
        : null
    default:
      return null
  }
}

export const AGENT_UI: BuilderUi = {
  nouns: {
    doc: "Workflow",
    docs: "Workflows",
    step: "node",
    steps: "nodes",
  },
  docIcon: AGENTS_ICON,
  listView: "agents",
  permission: "agents:manage",
  detailOf: (step, lookups) =>
    detailOf(step as AgentStep, lookups as AgentLookups),
  summaryOf: (step) => summaryOf(step as AgentStep),
  Fields: AgentFields as BuilderUi["Fields"],
  // A sub-agent's settings open in place of the node's, its own name with them.
  ownsName: true,
  // The library's groups, readable on the canvas: People and the ends keep their own looks.
  kindTones: { agents: "agent", actions: "action", logic: "logic" },
  kindLabels: true,
  DataSection: AgentDataSection as BuilderUi["DataSection"],
  emptyCanvas:
    "Drag nodes here from the library, or click one to add it. Begin with Start.",
  ready: "Every node is set up and reached from the start.",
}
