import * as React from "react"

import { codeGraphApi, graphErrorMessage } from "../lib/api"
import { mergeGraph, type GraphData, type NodeVersion } from "../lib/graph"

/** What the explorer is loading, if anything. */
export type GraphBusy = "" | "initial" | "neighbors" | "page"

/**
 * The part of a repository's graph being explored: a first page of nodes
 * (of one kind, or any), or just the node to open at, grown by expanding
 * nodes and loading more pages.
 * Every read after the first pins the generation the first one named, so
 * the explorer never mixes revisions. Leaving (or changing the kind)
 * cancels whatever is in flight.
 *
 * It keeps its own state rather than a query cache: what's loaded is the
 * user's exploration, merged from many reads, not one server response.
 */
export function useCodeGraph(
  organizationId: string,
  repositoryId: string,
  kind: string,
  openAt?: string
) {
  const graph = React.useMemo(
    () => codeGraphApi(organizationId, repositoryId),
    [organizationId, repositoryId]
  )
  const [data, setData] = React.useState<GraphData | null>(null)
  const [busy, setBusy] = React.useState<GraphBusy>("initial")
  const [error, setError] = React.useState("")
  // A node's next neighbour page; null once all are loaded.
  const [cursors, setCursors] = React.useState<Record<string, string | null>>(
    {}
  )
  const controller = React.useRef<AbortController | null>(null)
  const working = React.useRef(false)

  React.useEffect(() => {
    const abort = new AbortController()
    controller.current = abort
    graph
      .view(kind, 0, "", abort.signal)
      .then(async (result) => {
        if (openAt && result.generation > 0) {
          // Start from the node alone, at the generation just read.
          try {
            const node = await graph.node(
              openAt,
              result.generation,
              abort.signal
            )
            result = {
              ...result,
              nodes: [node],
              edges: [],
              next_cursor: undefined,
              truncated: false,
            }
          } catch {
            if (!abort.signal.aborted) {
              setError(
                "The declaration you followed isn't in this graph any more; showing a sample instead."
              )
            }
          }
        }
        if (!abort.signal.aborted) setData(result)
      })
      .catch((e: unknown) => {
        if (!abort.signal.aborted) setError(graphErrorMessage(e))
      })
      .finally(() => {
        if (!abort.signal.aborted) setBusy("")
      })
    return () => abort.abort()
  }, [graph, kind, openAt])

  // One read at a time, so merges never race.
  async function run(
    name: GraphBusy,
    action: (signal: AbortSignal) => Promise<void>
  ) {
    const signal = controller.current?.signal
    if (working.current || !signal || signal.aborted) return
    working.current = true
    setBusy(name)
    setError("")
    try {
      await action(signal)
    } catch (e) {
      if (!signal.aborted) setError(graphErrorMessage(e))
    } finally {
      working.current = false
      if (!signal.aborted) setBusy("")
    }
  }

  const expand = (node: NodeVersion) =>
    run("neighbors", async (signal) => {
      const id = node.fact.node.id
      if (!data || cursors[id] === null) return
      const page = await graph.neighbors(
        id,
        data.generation,
        cursors[id] ?? "",
        signal
      )
      if (signal.aborted) return
      setData(
        mergeGraph(data, {
          repository_id: data.repository_id,
          generation: page.generation,
          nodes: [
            node,
            ...page.neighbors.flatMap((n) => (n.node ? [n.node] : [])),
          ],
          edges: page.neighbors.map((n) => n.edge),
          truncated: !!page.next_cursor,
        })
      )
      setCursors((current) => ({ ...current, [id]: page.next_cursor || null }))
    })

  const more = () =>
    run("page", async (signal) => {
      if (!data?.next_cursor) return
      const page = await graph.view(
        kind,
        data.generation,
        data.next_cursor,
        signal
      )
      if (signal.aborted) return
      setData({ ...mergeGraph(data, page), next_cursor: page.next_cursor })
    })

  /** Adds a node found another way, e.g. by name. */
  function add(node: NodeVersion) {
    if (data) setData(mergeGraph(data, { ...data, nodes: [node], edges: [] }))
  }

  /** Starts over from one node. */
  function focus(node: NodeVersion) {
    if (data) {
      setData({
        ...data,
        nodes: [node],
        edges: [],
        next_cursor: undefined,
        truncated: false,
      })
    }
    setCursors({})
  }

  return { data, busy, error, expand, more, add, focus, cursors, graph }
}
