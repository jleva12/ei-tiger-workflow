import { ApiError } from "@/lib/api"
import { api } from "@/lib/api-instance"
import type { GraphData, Neighbors, NodeSourceText, NodeVersion } from "./graph"

/*
 * The admin API's code graph reads for one of an organization's code
 * repositories. It passes the code graph worker's answers through
 * unchanged, at the generation asked for (0 for the live one). Reads answer
 * 409 before the repository's first successful ingestion, and 503 when the
 * worker doesn't answer.
 */

/** Where a repository's reads are, under the organization's repositories. */
export const repositoryPath = (organizationId: string, repositoryId: string) =>
  `/organizations/${encodeURIComponent(organizationId)}/code-repositories/${encodeURIComponent(repositoryId)}`

/** The admin API's code graph reads for one code repository. */
export function codeGraphApi(organizationId: string, repositoryId: string) {
  const base = `${repositoryPath(organizationId, repositoryId)}/graph`
  return {
    /** A page of nodes, of one kind or any, with the edges among them. */
    view: (
      kind: string,
      generation: number,
      cursor: string,
      signal?: AbortSignal
    ) =>
      api.get<GraphData>(base, {
        params: { kind, generation, cursor },
        signal,
      }),
    neighbors: (
      node: string,
      generation: number,
      cursor: string,
      signal?: AbortSignal
    ) =>
      api.get<Neighbors>(`${base}/neighbors`, {
        params: { node, generation, direction: "both", limit: 40, cursor },
        signal,
      }),
    symbols: (name: string, generation: number, signal?: AbortSignal) =>
      api.get<{ nodes: NodeVersion[] }>(`${base}/symbols`, {
        params: { name, generation, limit: 20 },
        signal,
      }),
    source: (node: string, generation: number, signal?: AbortSignal) =>
      api.get<NodeSourceText>(`${base}/source`, {
        params: { node, generation, context: 3 },
        signal,
      }),
    /** One node, e.g. to open the explorer at it. */
    node: (node: string, generation: number, signal?: AbortSignal) =>
      api.get<NodeVersion>(`${base}/node`, {
        params: { node, generation },
        signal,
      }),
  }
}

/** The reads one explorer makes, for its finder and inspector. */
export type CodeGraphReads = ReturnType<typeof codeGraphApi>

/**
 * Query keys, so the explorer's reads cache per repository and generation.
 * Not under the repositories' keys: a generation never changes, so an
 * ingestion has nothing of it to refresh.
 */
export const codeGraphKeys = {
  all: ["code-graph"] as const,
  repository: (organizationId: string, repositoryId: string) =>
    [...codeGraphKeys.all, organizationId, repositoryId] as const,
  symbols: (
    organizationId: string,
    repositoryId: string,
    generation: number,
    name: string
  ) =>
    [
      ...codeGraphKeys.repository(organizationId, repositoryId),
      generation,
      "symbols",
      name,
    ] as const,
  source: (
    organizationId: string,
    repositoryId: string,
    generation: number,
    node: string
  ) =>
    [
      ...codeGraphKeys.repository(organizationId, repositoryId),
      generation,
      "source",
      node,
    ] as const,
}

/**
 * Why a graph read failed, in words: the two answers the admin API gives
 * for the graph itself rather than the request, and otherwise the server's
 * own message.
 */
export function graphErrorMessage(error: unknown) {
  if (error instanceof ApiError) {
    if (error.status === 409) {
      return "Nothing is published for this repository yet. Its graph opens here once an ingestion succeeds."
    }
    if (error.status === 503) {
      return "The code graph worker didn't answer. Try again in a moment."
    }
  }
  return error instanceof Error ? error.message : String(error)
}
