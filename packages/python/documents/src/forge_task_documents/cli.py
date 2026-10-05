"""The documents task's commands in the worker's CLI (``forge-async-worker docs ...``).

forge-async-worker docs ingest --tenant t1 docs/*.docx
forge-async-worker docs search --tenant t1 "refund approval limit"
forge-async-worker docs chunks path/to/file.xlsx          # preview chunking, no DB
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from forge_tasks.tasks import JobSpec


def _print(obj: Any) -> None:
    print(json.dumps(obj, indent=2, default=str))


def add_commands(parser: argparse.ArgumentParser) -> None:
    docs = parser.add_subparsers(dest="action", required=True)
    di = docs.add_parser("ingest")
    di.add_argument("--tenant", required=True)
    di.add_argument("--force", action="store_true")
    di.add_argument("paths", nargs="+", type=Path)
    ds = docs.add_parser("search")
    ds.add_argument("--tenant", required=True)
    ds.add_argument("--top-k", type=int, default=8)
    ds.add_argument("text")
    dc = docs.add_parser("chunks")
    dc.add_argument("path", type=Path)


async def run_command(args: argparse.Namespace, rt: Any) -> None:
    if args.action == "chunks":
        from forge_embeddings.clients import model_clients
        from forge_task_documents.chunking import ChunkEngine
        from forge_task_documents.models import SourceFile
        from forge_task_documents.parsers import default_registry as parsers

        src = SourceFile(tenant_id="preview", doc_id="preview", filename=args.path.name, data=args.path.read_bytes())
        parsed = parsers().resolve(src).parse(src)
        tokenizer = model_clients(rt.context).tokenizer
        for c in ChunkEngine(tokenizer, rt.documents.settings.chunking).chunk(parsed):
            print(f"--- #{c.ordinal} {c.kind} tokens={c.token_count} path={' > '.join(c.section_path)}")
            print(c.text)
    elif args.action == "ingest":
        for p in args.paths:
            doc_id = hashlib.sha1(str(p.resolve()).encode()).hexdigest()[:16]
            spec = JobSpec(
                task_type="documents",
                kind="ingest",
                payload={"tenant_id": args.tenant, "doc_id": doc_id, "uri": str(p), "force": args.force},
            )
            _print((await rt.submit(spec)).model_dump(mode="json"))
    elif args.action == "search":
        from forge_task_documents.models import SearchQuery

        resp = await rt.documents.search.search(SearchQuery(tenant_id=args.tenant, text=args.text, top_k=args.top_k))
        for i, h in enumerate(resp.hits, 1):
            print(f"{i}. [{h.chunk.kind}] {h.chunk.title} > {h.chunk.section_text}  rrf={h.score:.4f}")
            print("   " + h.chunk.text[:200].replace("\n", " | "))
