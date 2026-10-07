# forge-embeddings

Model clients and search helpers shared by the embedding tasks
([documents](../documents/README.md), [commits](../commits/README.md)):
OpenAI, Voyage and hashing embedders, rerankers, tokenizers, Anthropic and
OpenAI LLM clients, BM25 and reciprocal-rank fusion, identifier extraction,
and BSON vector encoding for MongoDB (`import forge_embeddings`).

A task asks for the process's shared clients with
`forge_embeddings.clients.model_clients(ctx)`; they are configured by
`HYBRID_EMBEDDING__*`, `HYBRID_EMBEDDING_PROFILES`, `HYBRID_RERANK__*`,
`HYBRID_TOKENIZER__*` and `HYBRID_LLM__*`.

## Configuration

Read from the environment and the working directory's `.env`, like every
`HYBRID_*` setting (`ModelSettings` in `config.py`). Put API keys in `.env` as
`HYBRID_EMBEDDING__API_KEY` and `HYBRID_LLM__API_KEY` (usually `${OPENAI_API_KEY}`
references to `.env.common`): the `OPENAI_API_KEY`, `AZURE_OPENAI_API_KEY`,
`VOYAGE_API_KEY` and `ANTHROPIC_API_KEY` fallbacks are only seen as real
environment variables.

In this repository the apps set the model, dimensions and base URL from the
shared `FORGE_EMBEDDING_*` in `.env.common`
(`HYBRID_EMBEDDING__DOCUMENT_MODEL=${FORGE_EMBEDDING_MODEL}`), which the code
graph embeds with too; empty strings count as unset.

```
HYBRID_EMBEDDING__PROVIDER=openai   HYBRID_EMBEDDING__API_KEY=...   # the default embedder: OpenAI
HYBRID_EMBEDDING__DOCUMENT_MODEL=text-embedding-3-large   HYBRID_EMBEDDING__DIMENSIONS=1024
#   Azure OpenAI instead: HYBRID_EMBEDDING__AZURE_ENDPOINT=https://<res>.openai.azure.com
#   HYBRID_EMBEDDING__DOCUMENT_MODEL=<deployment>  (HYBRID_EMBEDDING__API_VERSION optional)
#   Gateway/proxy: HYBRID_EMBEDDING__BASE_URL=https://...
#   Voyage: HYBRID_EMBEDDING__PROVIDER=voyage with the voyage extra
#   Offline: HYBRID_EMBEDDING__PROVIDER=hashing (no semantic search)
HYBRID_RERANK__PROVIDER=none                         # default; "voyage" with a Voyage key (HYBRID_RERANK__API_KEY)
HYBRID_EMBEDDING_PROFILES='{"small": {"document_model": "text-embedding-3-small", "dimensions": 512}}'  # optional
HYBRID_LLM__PROVIDER=openai   HYBRID_LLM__MODEL=gpt-6-luna   HYBRID_LLM__API_KEY=${OPENAI_API_KEY}
HYBRID_LLM__REASONING_EFFORT=low     # openai reasoning models; they take no temperature
#   Claude instead: HYBRID_LLM__PROVIDER=anthropic HYBRID_LLM__MODEL=claude-haiku-4-5-20251001
```

`eval.py` is a Recall@k / MRR@10 harness with an ablation runner, for tuning a
task's search against labeled questions.
