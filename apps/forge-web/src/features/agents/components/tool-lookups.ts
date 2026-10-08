import * as React from "react"

import { useOrganizationKnowledgeBases } from "@/features/knowledge/lib/api"
import { useOrganizationMcpServers } from "@/features/mcp-servers/lib/api"
import type { KnowledgeBaseLookup, McpServerLookup } from "./chat-agent-store"

/*
 * The organization's MCP servers and knowledge bases, as tools pick them
 * (their names, an MCP server's tools) and as their checks see them (whether
 * a server is signed in to, how many of a knowledge base's documents are
 * searchable): for the Agents builder's tool nodes and a workflow's LLM
 * agents' tools alike.
 */

export type ToolChecks = {
  /** Unknown until they load (or when they can't be read). */
  mcpServers?: Map<
    string,
    { name: string; connected: boolean; tools: string[] | null }
  >
  knowledgeBases?: Map<
    string,
    { name: string; kind: "rag" | "system"; ready: number }
  >
}

export function useToolLookups(organizationId: string): {
  lookups: {
    mcpServers: Record<string, McpServerLookup>
    knowledgeBases: Record<string, KnowledgeBaseLookup>
  }
  checks: ToolChecks
} {
  const mcpServers = useOrganizationMcpServers(organizationId)
  const knowledgeBases = useOrganizationKnowledgeBases(organizationId)
  return React.useMemo(() => {
    const servers = mcpServers.data ?? []
    const bases = knowledgeBases.data ?? []
    const tools = (s: (typeof servers)[number]) =>
      s.checked_at ? s.tools.map((t) => t.name) : null
    return {
      lookups: {
        mcpServers: Object.fromEntries(
          servers.map((s) => [
            s.id,
            { name: s.name, url: s.url, tools: tools(s) },
          ])
        ),
        knowledgeBases: Object.fromEntries(
          bases.map((kb) => [
            kb.id,
            {
              name: kb.name,
              description: kb.description,
              kind: kb.kind,
              // A system design knowledge base's repositories, and those ingested.
              documents: kb.kind === "system" ? kb.repositories : kb.documents,
              ready: kb.kind === "system" ? kb.ingested : kb.ready,
            },
          ])
        ),
      },
      checks: {
        mcpServers: mcpServers.isSuccess
          ? new Map(
              servers.map((s) => [
                s.id,
                {
                  name: s.name,
                  connected: !s.auth.interactive || s.auth.connected === true,
                  tools: tools(s),
                },
              ])
            )
          : undefined,
        knowledgeBases: knowledgeBases.isSuccess
          ? new Map(
              bases.map((kb) => [
                kb.id,
                {
                  name: kb.name,
                  kind: kb.kind,
                  ready: kb.kind === "system" ? kb.ingested : kb.ready,
                },
              ])
            )
          : undefined,
      },
    }
  }, [
    mcpServers.data,
    mcpServers.isSuccess,
    knowledgeBases.data,
    knowledgeBases.isSuccess,
  ])
}
