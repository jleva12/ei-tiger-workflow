import * as React from "react"
import { createFileRoute } from "@tanstack/react-router"

import {
  KnowledgePage,
  type SearchChange,
} from "@/features/knowledge/components/knowledge-page"
import {
  isKindKey,
  isKnowledgeShelf,
  type KnowledgeSearch,
} from "@/features/knowledge/lib/knowledge"

/**
 * One of an organization's knowledge bases: what was uploaded to it for the
 * organization's agents to search, or a graph one's code repositories. Not
 * nested in the workspace's page (`$organizationId_`), because it draws its
 * own sidebar in place of the workspace's. Only the organization's members
 * get in. `document` opens one in the viewer over the page, so it can be
 * linked to; `repository` (with `section` and `node`) a graph one's
 * repository, at a node of its code graph.
 */
export const Route = createFileRoute(
  "/organizations/$organizationId_/knowledge/$knowledgeBaseId"
)({
  validateSearch: (search: Record<string, unknown>): KnowledgeSearch => {
    const shelf = isKnowledgeShelf(search.shelf) ? search.shelf : undefined
    // A shelf and a collection are both places; the shelf wins.
    const collection =
      !shelf && typeof search.collection === "string" && search.collection
        ? search.collection
        : undefined
    const q = typeof search.q === "string" ? search.q.trim() : ""
    return {
      shelf,
      collection,
      kind: isKindKey(search.kind) ? search.kind : undefined,
      q: q || undefined,
      layout: search.layout === "grid" ? "grid" : undefined,
      document:
        typeof search.document === "string" &&
        /^[A-Za-z0-9-]{1,36}$/.test(search.document)
          ? search.document
          : undefined,
      repository:
        typeof search.repository === "string" &&
        /^[A-Za-z0-9-]{1,36}$/.test(search.repository)
          ? search.repository
          : undefined,
      section: search.section === "ingestion" ? "ingestion" : undefined,
      node:
        typeof search.node === "string" &&
        search.node.length > 0 &&
        search.node.length <= 1024
          ? search.node
          : undefined,
    }
  },
  component: KnowledgeBaseRoute,
})

function KnowledgeBaseRoute() {
  const { organizationId, knowledgeBaseId } = Route.useParams()
  const search = Route.useSearch()
  const navigate = Route.useNavigate()
  const onSearch = React.useCallback<SearchChange>(
    (patch, { replace } = {}) =>
      void navigate({ search: (prev) => ({ ...prev, ...patch }), replace }),
    [navigate]
  )
  return (
    <KnowledgePage
      key={knowledgeBaseId}
      organizationId={organizationId}
      knowledgeBaseId={knowledgeBaseId}
      search={search}
      onSearch={onSearch}
    />
  )
}
