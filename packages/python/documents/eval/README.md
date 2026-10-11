# Retrieval evaluation

A labelled suite of documents and questions, for telling whether a change to
the embedding model, chunking, parsers or search settings makes the knowledge
bases' search better or worse.

A run ingests the suite's documents through the worker's real pipeline
(parse → chunk → enrich → embed). It then asks every question through
`KnowledgeBaseSearch`, the search agents use, and scores the passages against
the labels. `compare` puts two runs side by side.

```sh
make docs-eval NAME=baseline                                    # today's settings
make docs-eval NAME=small SET="embedding.document_model=text-embedding-3-small"
make docs-eval-compare A=baseline B=small MAX_DROP=0.02         # exit 1 if hit@5 drops > 2 pts
```

or directly, from `apps/forge-async-worker`:

```sh
uv run forge-async-worker docs eval check ../../packages/python/documents/eval/suites/core
uv run forge-async-worker docs eval run   ../../packages/python/documents/eval/suites/core --name baseline
uv run forge-async-worker docs eval compare <baseline.json> <candidate.json> [--k 5] [--max-drop 0.02]
```

A run takes about half a minute with OpenAI embeddings. Its report goes to
`suites/<suite>/results/<name>.json`, with a Markdown summary beside it;
`results/` isn't committed. `suites/core/baselines/` keeps reference runs
worth keeping. To compare against one, pass its path to `docs eval compare`.

## What it changes

Every run starts from the worker's settings (`apps/forge-async-worker/.env`,
`HYBRID_*`). `--set key=value` (repeatable; `SET="a=1 b=2"` in make) changes
one setting for the run:

| key | changes |
|---|---|
| `embedding.*` (`document_model`, `dimensions`, `provider`, `base_url`) | the embedding model, for chunks and questions |
| `rerank.provider` (`none`, `overlap`, `voyage`), `rerank.model` | the reranker |
| `tokenizer.kind` | how chunks are measured |
| `chunking.*` (`max_tokens`, `min_tokens`, `merge_peers`, …) | the chunker |
| `search.*` (`vector_weight`, `text_weight`, `candidate_k`, `min_similarity`, …) | the hybrid search |
| `context.enabled=true` | LLM chunk context (one LLM call per chunk) |
| `pdf_parser=docling`, `docx_parser=docling` | the parsers |

`--profile NAME` embeds with a named `HYBRID_EMBEDDING_PROFILES` profile.
Values are read as JSON when they parse (`null`, `0.3`, `true`); anything
else is a string.

**Backends.** The default, `--backend memory`, keeps everything in the
process: any embedding model or dimension works, nothing is left behind, and
no services need to be running. Its BM25 imitates Atlas Search's
`lucene.english`. To measure Atlas Search and Vector Search themselves
(`storage/mongo/pipelines.py`, the indexes, `fusion_mode`), use
`EVAL_ARGS="--backend configured"`. That ingests into the worker's Mongo
under a throwaway knowledge base, waits for the search indexes to catch up,
and deletes it after the run (unless `--keep`). Each document leaves a small
tombstone record, as any deleted document does. It's limited to the vector
index's dimensions.

**Answers.** `EVAL_ARGS=--answers` also has the LLM (`HYBRID_LLM__*`) answer
each question from the top 5 passages, as an agent would. A second LLM call
grades that answer against the label: correct, partial or incorrect. That's
two calls a question; the scores are under `answer score` in the report.

## What the scores mean

For each answerable question, scored over the passages the search returned,
best first:

| metric | the question counts when… |
|---|---|
| `hit@k` | **every** evidence item is in the top k passages: an agent reading k passages has everything it needs |
| `recall@k` | (the share of its evidence items in the top k) |
| `right doc@k` | every document its evidence comes from has some passage in the top k |
| `MRR` | (mean of 1 / rank of each evidence item's first passage) |
| `answered nothing` | the search returned no passages at all (the relevance floor cut everything) |
| `abstained` | for an unanswerable question, the search returned nothing: what it *should* do |

`right doc` high but `hit` low means the right document was found, but not
the right part of it. The passage was too small or too fragmentary to hold
the facts, which points to chunking. `right doc` low points to embedding or
search instead.

Results are broken down by question kind and by file format. `compare` adds
which settings differ, every question that regressed or improved at hit@k,
and every rank change.

## The core suite

Ten documents from one fictional insurer, one per format:

| document | format | what it tests |
|---|---|---|
| claims manual | `.md` | an authority matrix nested region → role → line of business, where every leaf reads alike; a numbered procedure with sub-steps; red/amber flags |
| travel and expense policy | `.docx` | headings, a lodging table, numbered and bulleted lists |
| auto premium rates | `.xlsx` | 36 rate rows split into row groups plus a summary; a discounts sheet |
| incident runbook | `.html` | page chrome to drop, a severity table, nested lists, a code block |
| Q3 business review | `.pptx` | nested bullets, speaker notes, a table, chart data |
| vendor agreement | `.pdf` | numbered clauses, a ruled fee table, a running header |
| adjuster roster | `.csv` | rows split into groups with repeated headers |
| release notes | `.txt` | versions and ticket ids (`CC-4821`) |
| FNOL intake flow | `.mmd` | a Mermaid flowchart with decisions |
| payment approval | `.vsdx` | a Visio diagram with a labelled connector and a note |

There are 80 questions. Their kinds are `fact`, `paraphrase` (worded unlike
the document), `table`, `hierarchy`, `procedure`, `diagram`, `identifier`,
`code`, `notes` (speaker notes), `multi_doc` (needs passages from two
documents) and `unanswerable` (the documents don't say).

## Adding questions

In `suites/core/questions.yaml`:

```yaml
- id: rates-ohio-premium          # stable: compare matches questions by id
  kind: table
  question: What is the monthly premium for the Premium plan in Ohio?
  answer: 158 USD a month.        # graded against with --answers
  evidence:                       # every item must be found for a hit
    - doc: premium-rates          # a document id from suite.yaml
      contains: ["State: Ohio; Plan: Premium; Monthly premium (USD): 158"]
```

- **Labels are text, not chunk ids.** One passage of that document must hold
  every `contains` string (case, spacing, quotes and dashes are folded). That
  way one label scores any chunking.
- **Include the context that makes the passage self-sufficient.** For a
  nested bullet, also list its parents (`["EMEA", "Field Adjuster", "up to
  8,000 EUR"]`). A passage holding only `up to 8,000 EUR` doesn't say whose
  limit it is, and shouldn't count.
- **Match what the parser produces, not the source file.** Table rows read
  `Column: value; Column: value`, and diagram edges read `[A] --label--> [B]`.
  Preview a file with `uv run forge-async-worker docs chunks <file>`.
- **Alternatives.** When a fact is stated in two places, use
  `- any: [{doc: a, contains: [...]}, {doc: b, contains: [...]}]`.
- **Unanswerable questions.** Use `answerable: false` and no `evidence`.
  Near misses ("SEV5", "version 9.0") are more telling than off-topic ones.
- **Check after editing.** Run `make docs-eval-check`. It parses and chunks
  every document (no embedding) and lists any label no chunk can satisfy.
  `tests/test_evaluation.py` runs the same check and a whole offline run (the
  hashing embedder) with the tests.

Changing a question or a document changes the suite's fingerprint, and
`compare` warns when two runs used different versions. Re-run the baseline
after editing the suite.

To change the documents, edit `build_corpus.py` and run
`uv run python eval/build_corpus.py` from `packages/python/documents`. The
claims manual is the exception: it's plain Markdown, edited directly.

A new suite is another directory under `suites/` with its own `suite.yaml`,
`questions.yaml` and files. Run it with `SUITE=<name>`.
