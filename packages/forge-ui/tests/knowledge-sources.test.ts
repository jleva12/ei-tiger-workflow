import assert from "node:assert/strict"
import { test } from "node:test"
import { knowledgeBaseSources } from "../src/components/forge/assistant/knowledge-sources.ts"

const passage = {
  ref: "KQM4821",
  knowledge_base: "Claims knowledge",
  knowledge_base_id: "kb-claims",
  document: "Appeals guide.pdf",
  document_id: "doc-appeals",
  section: "Appeals > Denied claims",
  location: "page 4",
  text: "To appeal a denied claim, file within 180 days.",
  score: 0.61,
}
const search = { searched: ["Claims knowledge"], passages: [passage] }

test("a Forge tool's result is read from its envelope, bare, as JSON text or wrapped", () => {
  const sources = knowledgeBaseSources()
  for (const result of [
    { status: "success", payload: search },
    search,
    JSON.stringify({ status: "success", payload: search }),
    { result: { status: "success", payload: search } },
  ]) {
    const [source] = sources.fromToolCall("claims_lookup", result)
    assert.deepEqual(source, {
      ref: "KQM4821",
      document: {
        key: "doc-appeals",
        title: "Appeals guide.pdf",
        subtitle: "Claims knowledge",
        href: undefined,
      },
      location: "Appeals > Denied claims (page 4)",
      excerpt: "To appeal a denied claim, file within 180 days.",
    })
  }
})

test("other tools' results, and failures, cite nothing", () => {
  const sources = knowledgeBaseSources()
  assert.deepEqual(
    sources.fromToolCall("calculate", {
      status: "success",
      payload: { result: 4 },
    }),
    []
  )
  assert.deepEqual(
    sources.fromToolCall("search", { status: "failed", reason: "no" }),
    []
  )
  assert.deepEqual(sources.fromToolCall("search", "not json"), [])
  assert.equal(sources.searchOf?.("calculate", { result: 4 }), undefined)
})

test("a search says what it searched and found", () => {
  const sources = knowledgeBaseSources()
  const two = { ...passage, ref: "BTR0042", location: "page 5" }
  const other = { ...passage, ref: "MBR0001", document_id: "doc-benefits" }
  assert.deepEqual(
    sources.searchOf?.("search", {
      status: "success",
      payload: { ...search, passages: [passage, two, other] },
    }),
    { searched: ["Claims knowledge"], found: 3, documents: 2 }
  )
  // Nothing relevant: still a search, which found nothing.
  assert.deepEqual(
    sources.searchOf?.("search", {
      status: "success",
      payload: { searched: ["Claims knowledge"], passages: [], note: "…" },
    }),
    { searched: ["Claims knowledge"], found: 0, documents: 0 }
  )
})

test("a document links where the app says, with the search's organization", () => {
  const sources = knowledgeBaseSources({
    documentHref: (p) =>
      p.organization_id
        ? `/organizations/${p.organization_id}/knowledge/${p.knowledge_base_id}?document=${p.document_id}`
        : undefined,
  })
  const [linked] = sources.fromToolCall("search_knowledge_bases", {
    status: "success",
    payload: { ...search, organization_id: "org-1" },
  })
  assert.equal(
    linked?.document.href,
    "/organizations/org-1/knowledge/kb-claims?document=doc-appeals"
  )
  // A chat agent's tool doesn't say: no link.
  const [plain] = sources.fromToolCall("claims_lookup", search)
  assert.equal(plain?.document.href, undefined)
})
