import type { IconProp } from "@/components/forge/icons"
import { CODE_REPOSITORIES_ICON } from "@/features/code-repositories/lib/display"
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
  graph: {
    label: "Graph knowledge base",
    short: "Graph",
    summary: "Code repositories your agents search, as a code graph",
    icon: CODE_REPOSITORIES_ICON,
  },
}
