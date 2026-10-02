import type { ChatAgentDocument } from "@/features/agents/lib/document"
import { slugify } from "@/features/steps/lib/model"

/** The file an agent downloads as: `support-assistant.chat-agent.json`. */
export const chatAgentFileName = (doc: Pick<ChatAgentDocument, "name">) =>
  `${slugify(doc.name).replaceAll("_", "-") || "agent"}.chat-agent.json`
