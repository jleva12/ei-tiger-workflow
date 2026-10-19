"""The documents task's commands in the worker's CLI (``forge-async-worker docs ...``).

forge-async-worker docs ingest --tenant t1 docs/*.docx
forge-async-worker docs search --tenant t1 "refund approval limit"
forge-async-worker docs search --tenant t1 --tenant t2 "refund approval limit"   # both, ranked together
forge-async-worker docs chunks path/to/file.xlsx          # preview chunking, no DB

forge-async-worker docs eval check eval/suites/core       # every label holds in some chunk
forge-async-worker docs eval run eval/suites/core --name baseline
forge-async-worker docs eval run eval/suites/core --name small --set embedding.document_model=text-embedding-3-small
forge-async-worker docs eval compare eval/suites/core/results/baseline.json eval/suites/core/results/small.json
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
    ds.add_argument("--tenant", required=True, action="append", help="repeat to search several tenants together")
    ds.add_argument("--top-k", type=int, default=8)
    ds.add_argument("text")
    dc = docs.add_parser("chunks")
    dc.add_argument("path", type=Path)
    _add_eval_commands(docs.add_parser("eval", help="labelled retrieval evaluation (eval/README.md)"))


def _add_eval_commands(parser: argparse.ArgumentParser) -> None:
    ev = parser.add_subparsers(dest="eval_action", required=True)

    def settings_args(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--set",
            dest="overrides",
            action="append",
            default=[],
            metavar="KEY=VALUE",
            help="override a setting: chunking.max_tokens=300, search.min_similarity=0.3, "
            "embedding.document_model=text-embedding-3-small, rerank.provider=voyage (repeatable)",
        )
        p.add_argument("--profile", help="embed with this HYBRID_EMBEDDING_PROFILES profile")

    check = ev.add_parser("check", help="check every label is satisfiable by some chunk (no embedding)")
    check.add_argument("suite", type=Path)
    settings_args(check)

    run = ev.add_parser("run", help="ingest the suite, ask its questions, write a report")
    run.add_argument("suite", type=Path)
    run.add_argument("--name", required=True, help="the run's name; its report is results/<name>.json")
    run.add_argument("--backend", choices=["memory", "configured"], default="memory")
    run.add_argument("--k", type=int, action="append", help="cut-offs to score at (default 1, 3, 5, 10)")
    run.add_argument(
        "--answers", action="store_true", help="also have the LLM answer and grade it (2 calls a question)"
    )
    run.add_argument("--answer-k", type=int, default=5, help="passages the LLM answers from")
    run.add_argument("--out", type=Path, help="report path (default <suite>/results/<name>.json)")
    run.add_argument("--keep", action="store_true", help="leave the run's documents in configured storage")
    settings_args(run)

    compare = ev.add_parser("compare", help="compare two reports of one suite")
    compare.add_argument("baseline", type=Path)
    compare.add_argument("candidate", type=Path)
    compare.add_argument("--k", type=int, default=5, help="judge regressions at hit@k")
    compare.add_argument("--max-drop", type=float, help="exit 1 when hit@k drops by more than this (0.02 = 2 pts)")
    compare.add_argument("--out", type=Path, help="also write the comparison (Markdown) here")


async def _run_eval(args: argparse.Namespace, rt: Any) -> None:
    import sys

    from forge_embeddings.clients import build_models
    from forge_embeddings.config import ModelSettings
    from forge_task_documents import evaluation as ev
    from forge_task_documents.evaluation.report import DEFAULT_KS

    if args.eval_action == "compare":
        result = ev.compare(ev.Report.load(args.baseline), ev.Report.load(args.candidate), k=args.k)
        print(result.text)
        if args.out:
            args.out.write_text(result.text + "\n")
        if args.max_drop is not None and result.fails(args.max_drop):
            print(f"hit@{args.k} dropped {-result.hit_delta * 100:.1f} pts (allowed {args.max_drop * 100:.1f})")
            sys.exit(1)
        return

    suite = ev.load_suite(args.suite)
    overrides = ev.parse_overrides(args.overrides)
    documents, models = ev.apply_overrides(rt.documents.settings, ModelSettings(), overrides, profile=args.profile)
    if args.eval_action == "check":
        problems = ev.check_labels(suite, documents, build_models(models))
        for problem in problems:
            print(problem)
        print(f"{len(suite.cases)} questions, {len(problems)} unsatisfiable labels")
        if problems:
            sys.exit(1)
        return

    report = await ev.run_suite(
        suite,
        name=args.name,
        documents=documents,
        model_settings=models,
        overrides=overrides,
        backend=args.backend,
        storage=rt.documents.storage if args.backend == "configured" else None,
        ks=tuple(sorted(set(args.k))) if args.k else DEFAULT_KS,
        answers=args.answers,
        answer_k=args.answer_k,
        keep=args.keep,
        progress=lambda msg: print(msg, file=sys.stderr),
    )
    out = args.out or suite.root / "results" / f"{args.name}.json"
    report.save(out)
    text = ev.summary(report)
    out.with_suffix(".md").write_text(text)
    print(text)
    print(f"report: {out}")


async def run_command(args: argparse.Namespace, rt: Any) -> None:
    if args.action == "eval":
        await _run_eval(args, rt)
    elif args.action == "chunks":
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
        from forge_task_documents.models import DocumentRecord

        for p in args.paths:
            doc_id = hashlib.sha1(str(p.resolve()).encode()).hexdigest()[:16]
            # A fresh CLI invocation explicitly recreates a deleted path;
            # queued jobs themselves may never revive tombstones.
            if await rt.documents.storage.documents.get(args.tenant, doc_id) is None:
                await rt.documents.storage.documents.register(
                    DocumentRecord(
                        tenant_id=args.tenant,
                        doc_id=doc_id,
                        filename=p.name,
                        source_uri=str(p),
                        sha256="",
                        size_bytes=0,
                    )
                )
            spec = JobSpec(
                task_type="documents",
                kind="ingest",
                payload={"tenant_id": args.tenant, "doc_id": doc_id, "uri": str(p), "force": args.force},
            )
            _print((await rt.submit(spec)).model_dump(mode="json"))
    elif args.action == "search":
        from forge_task_documents.models import SearchQuery

        resp = await rt.documents.search.search(SearchQuery(tenant_ids=args.tenant, text=args.text, top_k=args.top_k))
        for i, h in enumerate(resp.hits, 1):
            print(f"{i}. [{h.chunk.kind}] {h.chunk.title} > {h.chunk.section_text}  rrf={h.score:.4f}")
            print("   " + h.chunk.text[:200].replace("\n", " | "))
