# forge-codegraph

The code graph for agents: the code tools the code graph MCP server and the
admin API's assistant both offer, over the repositories a caller's
organization reads; and system design knowledge bases, in the admin API (its
search route, hosted chat agents) and ADK workflows (LLM nodes) alike: their
code searched as knowledge base passages, and their designs described. The code
graph itself is read with [`forge-codegraph-query`](../codegraph-query/README.md).

| Module | Owns |
|---|---|
| `tools.py` | `CodeTools`: the code graph's tools as agents call them (`TOOLS`, `INSTRUCTIONS`), one definition each, which the code graph MCP server serves over MCP and the admin API's assistant as ADK tools |
| `scope.py` | `RepositoryScope`: the repositories a caller reads, its organization's, from the admin API's access answer (`from_access`), carried on a verified credential (`claims`, `from_claims`) |
| `passages.py` | `search_passages`: one search of knowledge bases' code (`Searched`, each with its `Repository`s), its hits as cited passages; `code_passages`: hits as the knowledge base tool's passages, cited like documents (`citation_ref`, the documents' algorithm); `interleave`: document and code passages merged by rank |
| `system.py` | a system design knowledge base's design in words: `design_summary` for the tool's description, `design_view` for the agents' design tool and the MCP server's `system_design` |

The caller decides which knowledge bases (and so repositories) a reader may
search, and passes only those.

```python
from forge_codegraph import Repository, Searched, search_passages
from forge_codegraph_query import CodeGraph

graph = CodeGraph.open(settings.codegraph)
passages = await search_passages(
    graph,
    [
        Searched(
            id=kb.id,
            name=kb.name,
            repositories=[
                Repository(
                    id=repo.id,
                    graph_id="repo:1Fb-ic-0MMj7iztdpYxEnQ",
                    name="pallets/itsdangerous",
                )
            ],
        )
    ],
    "how are tokens signed",
    limit=8,
)
```

```bash
make codegraph-check   # ruff, mypy, pytest
```
