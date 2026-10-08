# forge-codegraph

The [code graph worker](../../../apps/forge-codegraph-worker/README.md)'s read
and search API, for the admin API (system design knowledge bases, the code
graph explorer, their cross-repository links) and the async worker (workflow
LLM nodes searching system design knowledge bases).

| Module | Owns |
|---|---|
| `client.py` | `CodeGraph`: `read(repository, what, params)` for the published graph (`graph`, `neighbors`, `symbols`, `source`, `node`) and the ingestion audit (`stats`, `runs`); `search(repositories, query)` for code; `WorkerError` |
| `passages.py` | `code_passages`: search hits as the knowledge base tool's passages, cited like documents (`citation_ref`, the documents' algorithm); `interleave`: document and code passages merged by rank |

Every call sends the worker's API token (`CODEGRAPH_ADMISSION_TOKEN`). The
worker trusts its caller: the caller decides which repositories a reader may
see, and passes only those.

```python
from forge_codegraph import CodeGraph, Repository, code_passages

graph = CodeGraph.connect("http://127.0.0.1:8090", token)
hits = await graph.search(["repo:1Fb-ic-0MMj7iztdpYxEnQ"], "how are tokens signed")
passages = code_passages(
    hits,
    knowledge_base_id=kb.id,
    knowledge_base=kb.name,
    repositories=[
        Repository(
            id=repo.id,
            graph_id="repo:1Fb-ic-0MMj7iztdpYxEnQ",
            name="pallets/itsdangerous",
        )
    ],
)
```

```bash
make codegraph-check   # ruff, mypy, pytest
```
