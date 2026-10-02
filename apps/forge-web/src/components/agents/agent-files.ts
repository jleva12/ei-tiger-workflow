import type { AgentDocument } from "@/lib/agents/document"
import { slugify } from "@/lib/steps/model"

/** The file an agent downloads as: `support-desk.agent.json`. */
export const agentFileName = (doc: Pick<AgentDocument, "name">) =>
  `${slugify(doc.name).replaceAll("_", "-") || "adk-workflow"}.agent.json`
