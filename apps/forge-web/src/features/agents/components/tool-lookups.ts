import * as React from "react"

import {
  useCredentialAuthMethods,
  useOrganizationCredentials,
} from "@/features/credentials/lib/api"
import {
  lookupOf,
  type CredentialLookup,
} from "@/features/credentials/lib/display"
import { useOrganizationKnowledgeBases } from "@/features/knowledge/lib/api"
import { useOrganizationMcpServers } from "@/features/mcp-servers/lib/api"
import { useOrganizationSkills } from "@/features/skills/lib/api"
import type {
  KnowledgeBaseLookup,
  McpServerLookup,
  SkillLookup,
} from "./chat-agent-store"

/*
 * The organization's MCP servers, knowledge bases, API credentials and
 * skills, as tools pick them (their names, an MCP server's tools, where a credential
 * may go) and as their checks see them (whether a server or a credential is
 * signed in to, how many of a knowledge base's documents are searchable):
 * for the Agents builder's tool nodes, a workflow's LLM agents' tools and
 * its HTTP steps alike.
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
  credentials?: Map<string, CredentialLookup>
  skills?: Map<string, { name: string }>
}

export function useToolLookups(organizationId: string): {
  lookups: {
    mcpServers: Record<string, McpServerLookup>
    knowledgeBases: Record<string, KnowledgeBaseLookup>
    credentials: Record<string, CredentialLookup>
    skills: Record<string, SkillLookup>
  }
  checks: ToolChecks
} {
  const mcpServers = useOrganizationMcpServers(organizationId)
  const knowledgeBases = useOrganizationKnowledgeBases(organizationId)
  const credentialList = useOrganizationCredentials(organizationId)
  const methods = useCredentialAuthMethods()
  const skillList = useOrganizationSkills(organizationId)
  return React.useMemo(() => {
    const skills = skillList.data ?? []
    const servers = mcpServers.data ?? []
    const bases = knowledgeBases.data ?? []
    const methodNames = Object.fromEntries(
      (methods.data ?? []).map((m) => [m.kind, m.label])
    )
    const credentials = (credentialList.data ?? []).map(
      (c): [string, CredentialLookup] => [c.id, lookupOf(c, methodNames)]
    )
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
        credentials: Object.fromEntries(credentials),
        skills: Object.fromEntries(
          skills.map((s) => [
            s.id,
            { name: s.name, description: s.description },
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
        // Unknown until they load (or when they can't be read): a pick
        // isn't called deleted meanwhile.
        credentials: credentialList.isSuccess
          ? new Map(credentials)
          : undefined,
        skills: skillList.isSuccess
          ? new Map(skills.map((s) => [s.id, { name: s.name }]))
          : undefined,
      },
    }
  }, [
    mcpServers.data,
    mcpServers.isSuccess,
    knowledgeBases.data,
    knowledgeBases.isSuccess,
    credentialList.data,
    credentialList.isSuccess,
    methods.data,
    skillList.data,
    skillList.isSuccess,
  ])
}
