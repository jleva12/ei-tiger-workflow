import type {
  AssistantSearch,
  AssistantSource,
  AssistantSourcesConfig,
} from "./sources"

/*
 * Citations from Forge's knowledge base searches: a chat agent's knowledge
 * base tool (forge_agent_runtime's KnowledgeBaseTool) and the Forge
 * assistant's search_knowledge_bases answer
 *
 *   { status: "success",
 *     payload: { searched: ["Claims knowledge"],
 *                passages: [{ ref, knowledge_base, knowledge_base_id, document,
 *                             document_id, section, location, text, score }],
 *                organization_id?, note? } }
 *
 * (Forge's tools answer in that envelope; a bare payload reads too), and the
 * agent cites passages by ref ("[KQM4821]"). A chat agent's tool is named
 * whatever its builder called it, so a result is recognised by its shape,
 * not its name.
 */

/** One passage of a knowledge base tool's result. */
export type KnowledgePassage = {
  /** What the answer cites it by, e.g. "KQM4821". */
  ref: string
  /** The name of the knowledge base it's in. */
  knowledge_base?: string
  knowledge_base_id?: string
  /** The document's name, e.g. its filename. */
  document?: string
  document_id?: string
  /** The headings it's under: "Appeals > Denied claims". */
  section?: string
  /** Where in the document: "page 4", "slide 3: Pricing". */
  location?: string
  text?: string
  /** How relevant it is; higher is better. */
  score?: number
  /**
   * The organization whose knowledge base it is, when the search says
   * (the Forge assistant's does): for a link to the document.
   */
  organization_id?: string
}

export type KnowledgeSourcesOptions = {
  /**
   * Where a passage's document opens (the "Open document" link), e.g. its
   * knowledge base's page in Forge with it open; no link without.
   */
  documentHref?: (passage: KnowledgePassage) => string | undefined
}

const isObject = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value)

/**
 * A result as ADK delivers it: a value or JSON text, sometimes wrapped as
 * `{ result }`; a Forge tool's in `{ status: "success", payload }`.
 */
function readResult(result: unknown): Record<string, unknown> | undefined {
  let value = result
  if (typeof value === "string") {
    try {
      value = JSON.parse(value)
    } catch {
      return undefined
    }
  }
  if (
    isObject(value) &&
    isObject(value.result) &&
    Object.keys(value).length === 1
  )
    value = value.result
  if (isObject(value) && value.status === "success" && isObject(value.payload))
    value = value.payload
  return isObject(value) ? value : undefined
}

type KnowledgeResult = { searched: string[]; passages: KnowledgePassage[] }

/** A knowledge base search's result; undefined for any other tool's. */
function knowledgeResult(result: unknown): KnowledgeResult | undefined {
  const value = readResult(result)
  if (!value || !Array.isArray(value.passages)) return undefined
  const organization =
    typeof value.organization_id === "string"
      ? value.organization_id
      : undefined
  const passages = value.passages
    .filter(
      (p): p is KnowledgePassage => isObject(p) && typeof p.ref === "string"
    )
    .map((p) => ({ organization_id: organization, ...p }))
  const searched = Array.isArray(value.searched)
    ? value.searched.filter((name): name is string => typeof name === "string")
    : []
  return { searched, passages }
}

/** Identifies a passage's document: two knowledge bases can hold the same filename. */
const documentKey = (passage: KnowledgePassage) =>
  passage.document_id ||
  `${passage.knowledge_base_id ?? passage.knowledge_base ?? ""}/${passage.document ?? ""}`

/**
 * Where in its document a passage is: "Refunds > Eligibility (page 2)". The
 * sources list joins a document's passages with " · ", so one passage's
 * parts don't.
 */
function where({ section, location }: KnowledgePassage): string | undefined {
  if (section && location) return `${section} (${location})`
  return section || location || undefined
}

/**
 * The sources config for agents with Forge knowledge base tools: each
 * passage a search found becomes a source the answer can cite, grouped by
 * document, from the knowledge base named under it; each search call's card
 * says what it searched and found.
 *
 * ```tsx
 * const sources = knowledgeBaseSources()  // once, at module scope
 * <AssistantScreen runtime={runtime} sources={sources} />
 * ```
 */
export function knowledgeBaseSources(
  options: KnowledgeSourcesOptions = {}
): AssistantSourcesConfig {
  return {
    fromToolCall: (_toolName, result) =>
      (knowledgeResult(result)?.passages ?? []).map(
        (passage): AssistantSource => ({
          ref: passage.ref,
          document: {
            key: documentKey(passage),
            title: passage.document || "Untitled document",
            subtitle: passage.knowledge_base || undefined,
            href: options.documentHref?.(passage),
          },
          location: where(passage),
          excerpt: passage.text || undefined,
        })
      ),
    searchOf: (_toolName, result): AssistantSearch | undefined => {
      const found = knowledgeResult(result)
      if (!found) return undefined
      return {
        searched: found.searched,
        found: found.passages.length,
        documents: new Set(found.passages.map(documentKey)).size,
      }
    },
  }
}
