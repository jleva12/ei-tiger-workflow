import {
  knowledgeBaseSources,
  type KnowledgePassage,
} from "@/components/forge/assistant/index"

/**
 * Where a cited passage's document opens in Forge: its knowledge base's
 * page with the document open in the viewer. None without its organization
 * (a chat agent's tool doesn't say; the agent's own organization is then
 * given), knowledge base or document.
 */
export function knowledgeDocumentHref(
  passage: KnowledgePassage,
  organizationId: string | undefined = passage.organization_id
): string | undefined {
  const { knowledge_base_id: knowledgeBaseId, document_id: documentId } =
    passage
  if (!organizationId || !knowledgeBaseId || !documentId) return undefined
  return `/organizations/${encodeURIComponent(organizationId)}/knowledge/${encodeURIComponent(knowledgeBaseId)}?document=${encodeURIComponent(documentId)}`
}

/**
 * The Forge assistant's citations: the passages its knowledge base searches
 * found, numbered in its answers and listed under them, each opening its
 * document in Forge. One config, so each message's sources are read once.
 */
export const assistantSources = knowledgeBaseSources({
  documentHref: (passage) => knowledgeDocumentHref(passage),
})
