import type { AgentDocument } from "@/features/adk-workflows/lib/document"
import { slugify } from "@/features/steps/lib/model"

/** The file an agent downloads as: `support-desk.agent.json`. */
export const agentFileName = (doc: Pick<AgentDocument, "name">) =>
  `${slugify(doc.name).replaceAll("_", "-") || "adk-workflow"}.agent.json`
