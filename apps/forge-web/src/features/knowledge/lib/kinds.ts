import type { IconProp } from "@/components/forge/icons"
import { SYSTEM_ICON } from "./system"
import type { KnowledgeBaseKind } from "./api"
import { KNOWLEDGE_ICON } from "./knowledge"

/** Each kind of knowledge base, as the new knowledge base menu and the list name it. */
export const KNOWLEDGE_BASE_KINDS: Record<
  KnowledgeBaseKind,
  { label: string; short: string; summary: string; icon: IconProp }
> = {
  rag: {
    label: "RAG knowledge base",
    short: "RAG",
    summary: "Documents your agents read: runbooks, specs, policies",
    icon: KNOWLEDGE_ICON,
  },
  system: {
    label: "System design knowledge base",
    short: "System",
    summary: "Your applications, how they connect, and their code graphs",
    icon: SYSTEM_ICON,
  },
}
