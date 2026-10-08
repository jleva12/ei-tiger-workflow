import {
  knowledgeBaseSources,
  type KnowledgePassage,
} from "@/components/forge/assistant/index"

/** A system design knowledge base's passage: code, at a node of its repository's graph. */
type CodePassage = KnowledgePassage & {
  repository_id?: string
  node_id?: string
}

/**
 * Where a cited passage's document opens in Forge: its knowledge base's
 * page with the document open in the viewer, or, for a system design knowledge
 * base's code, its repository's code graph open at the declaration. None
 * without its organization (a chat agent's tool doesn't say; the agent's
 * own organization is then given), knowledge base or document.
 */
export function knowledgeDocumentHref(
  passage: KnowledgePassage,
  organizationId: string | undefined = passage.organization_id
): string | undefined {
  const {
    knowledge_base_id: knowledgeBaseId,
    document_id: documentId,
    repository_id: repositoryId,
    node_id: nodeId,
  } = passage as CodePassage
  if (!organizationId || !knowledgeBaseId) return undefined
  const page = `/organizations/${encodeURIComponent(organizationId)}/knowledge/${encodeURIComponent(knowledgeBaseId)}`
  if (repositoryId && nodeId)
    return `${page}?repository=${encodeURIComponent(repositoryId)}&node=${encodeURIComponent(nodeId)}`
  if (!documentId) return undefined
  return `${page}?document=${encodeURIComponent(documentId)}`
}

/**
 * The Forge assistant's citations: the passages its knowledge base searches
 * found, numbered in its answers and listed under them, each opening its
 * document in Forge. One config, so each message's sources are read once.
 */
export const assistantSources = knowledgeBaseSources({
  documentHref: (passage) => knowledgeDocumentHref(passage),
})
