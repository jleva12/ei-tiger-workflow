import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"

import {
  INGESTION_POLL_MS,
  isActive,
  organizationCodeRepositories,
  type CodeRepository,
  type CodeRepositoryCreate,
} from "@/features/code-repositories/lib/api"
import { api } from "@/lib/api-instance"
import { organizationKnowledgeBases, type KnowledgeScope } from "./api"

/*
 * A graph knowledge base's code repositories: some of the organization's
 * (its Code repositories page), each in any number of knowledge bases.
 * Including one adds a GitHub URL to the organization first when it doesn't
 * have it (queuing its first ingestion); removing one leaves it in the
 * organization, with its code graph. The list is keyed under the
 * organization's code repositories, so ingesting one, anywhere, refreshes it.
 */

const repositoriesPath = ({
  organizationId,
  knowledgeBaseId,
}: KnowledgeScope) =>
  `/organizations/${encodeURIComponent(organizationId)}/knowledge-bases/${encodeURIComponent(knowledgeBaseId)}/repositories`

/** One of the organization's repositories, or a GitHub repository to add to it. */
export type IncludeRepository =
  { repository_id: string } | Required<CodeRepositoryCreate>

const keyOf = (scope: KnowledgeScope) => [
  ...organizationCodeRepositories.scope({
    organizationId: scope.organizationId,
  }).keys.all,
  "knowledge-base",
  scope.knowledgeBaseId,
]

/**
 * The knowledge base's repositories, by URL, each with its latest ingestion
 * and its latest successful one; reread every few seconds while any
 * ingestion is queued or running.
 */
export function useKnowledgeBaseRepositories(scope: KnowledgeScope) {
  return useQuery({
    queryKey: keyOf(scope),
    queryFn: ({ signal }) =>
      api.get<CodeRepository[]>(repositoriesPath(scope), { signal }),
    refetchInterval: (query) =>
      query.state.data?.some((repository) =>
        isActive(repository.latest_ingestion?.status)
      )
        ? INGESTION_POLL_MS
        : false,
  })
}

/** Includes a repository in the knowledge base, and removes one from it. */
export function useKnowledgeBaseRepositoryMutations(scope: KnowledgeScope) {
  const queryClient = useQueryClient()
  // The organization's repositories (this list among them) and the
  // knowledge bases' counts.
  const refresh = () =>
    Promise.all([
      queryClient.invalidateQueries({
        queryKey: organizationCodeRepositories.scope({
          organizationId: scope.organizationId,
        }).keys.all,
      }),
      queryClient.invalidateQueries({
        queryKey: organizationKnowledgeBases.scope({
          organizationId: scope.organizationId,
        }).keys.all,
      }),
    ])
  const include = useMutation({
    mutationFn: (input: IncludeRepository) =>
      api.post<CodeRepository>(repositoriesPath(scope), input),
    // The dialogs show why it was refused.
    meta: { silent: true },
    onSuccess: refresh,
  })
  const remove = useMutation({
    mutationFn: (repositoryId: string) =>
      api.delete<void>(
        `${repositoriesPath(scope)}/${encodeURIComponent(repositoryId)}`
      ),
    meta: { errorTitle: "Couldn't remove the repository" },
    onSuccess: refresh,
  })
  return { include, remove }
}
