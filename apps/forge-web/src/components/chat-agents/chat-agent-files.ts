import type { ChatAgentDocument } from "@/lib/chat-agents/document"
import { slugify } from "@/lib/steps/model"

/** The file an agent downloads as: `support-assistant.chat-agent.json`. */
export const chatAgentFileName = (doc: Pick<ChatAgentDocument, "name">) =>
  `${slugify(doc.name).replaceAll("_", "-") || "agent"}.chat-agent.json`
