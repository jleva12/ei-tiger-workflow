"""Retrieval evaluation shared by every task type: Recall@k and MRR@10,
overall and per question kind, plus an ablation runner.

Eval set (JSONL), one case per line:
    {"question": "when did we stop polling for sessions?", "expected": ["<sha>"],
     "kind": "fuzzy", "params": {"repo": "acme/api"}}
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

SearchFn = Callable[["EvalCase"], Awaitable[list[str]]]  # returns ranked ids


class EvalCase(BaseModel):
    question: str
    expected: list[str] = Field(min_length=1)
    kind: str = "default"
    params: dict[str, Any] = Field(default_factory=dict)


class Metrics(BaseModel):
    n: int
    recall_at_k: float
    mrr_at_10: float


class EvalReport(BaseModel):
    k: int
    overall: Metrics
    by_kind: dict[str, Metrics]
    misses: list[dict[str, Any]] = Field(default_factory=list)


def load_cases(path: str | Path) -> list[EvalCase]:
    return [EvalCase.model_validate(json.loads(line)) for line in Path(path).read_text().splitlines() if line.strip()]


def _metrics(rows: list[tuple[bool, float]]) -> Metrics:
    n = len(rows)
    return Metrics(
        n=n,
        recall_at_k=round(sum(1 for hit, _ in rows if hit) / n, 4) if n else 0.0,
        mrr_at_10=round(sum(rr for _, rr in rows) / n, 4) if n else 0.0,
    )


async def evaluate(cases: Iterable[EvalCase], search: SearchFn, *, k: int = 5) -> EvalReport:
    per_kind: dict[str, list[tuple[bool, float]]] = defaultdict(list)
    overall: list[tuple[bool, float]] = []
    misses = []
    for case in cases:
        ranked = await search(case)
        expected = set(case.expected)
        hit = any(r in expected for r in ranked[:k])
        rr = next((1.0 / (i + 1) for i, r in enumerate(ranked[:10]) if r in expected), 0.0)
        per_kind[case.kind].append((hit, rr))
        overall.append((hit, rr))
        if not hit:
            misses.append({"question": case.question, "expected": case.expected, "got": ranked[:k]})
    return EvalReport(
        k=k, overall=_metrics(overall), by_kind={kd: _metrics(v) for kd, v in per_kind.items()}, misses=misses
    )


async def ablate(cases: list[EvalCase], variants: Mapping[str, SearchFn], *, k: int = 5) -> dict[str, EvalReport]:
    """Run the same cases against several search variants (e.g. dense-only,
    hybrid, hybrid without PR enrichment) to prove each piece earns its cost."""
    return {name: await evaluate(cases, fn, k=k) for name, fn in variants.items()}
