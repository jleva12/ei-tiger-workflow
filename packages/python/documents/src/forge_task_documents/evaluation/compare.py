"""Two runs of one suite, side by side: what changed, overall, per question
kind and document format, and question by question."""

from __future__ import annotations

from dataclasses import dataclass, field

from forge_task_documents.evaluation.report import CaseResult, Metrics, Report


@dataclass
class Comparison:
    text: str  # Markdown
    regressed: list[str] = field(default_factory=list)  # question ids hit before, missed now
    improved: list[str] = field(default_factory=list)
    hit_delta: float = 0.0  # overall hit@k, candidate minus baseline

    def fails(self, max_drop: float) -> bool:
        return self.hit_delta < -max_drop


def _pct(value: float | None) -> str:
    return "–" if value is None else f"{value * 100:.1f}%"


def _delta(a: float | None, b: float | None, *, points: bool = True) -> str:
    if a is None or b is None:
        return ""
    d = (b - a) * (100 if points else 1)
    if abs(d) < (0.05 if points else 0.00005):
        return "="
    arrow = "▲" if d > 0 else "▼"
    return f"{arrow} {d:+.1f}" if points else f"{arrow} {d:+.3f}"


def _setting_changes(a: Report, b: Report) -> list[str]:
    def flat(prefix: str, value: object, out: dict[str, object]) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                flat(f"{prefix}.{key}" if prefix else str(key), child, out)
        else:
            out[prefix] = value

    left: dict[str, object] = {}
    right: dict[str, object] = {}
    for side, report in ((left, a), (right, b)):
        flat("embedding", report.run.embedding, side)
        flat("", report.run.settings, side)
        side["rerank"] = report.run.rerank
        side["tokenizer"] = report.run.tokenizer
        side["chunker_version"] = report.run.chunker_version
        side["backend"] = report.run.backend
    return [
        f"`{k}`: {left.get(k)!r} → {right.get(k)!r}"
        for k in sorted(left.keys() | right.keys())
        if left.get(k) != right.get(k)
    ]


def _metric_rows(a: Metrics, b: Metrics, ks: list[str]) -> list[str]:
    rows = []
    for k in ks:
        rows.append(f"| hit@{k} | {_pct(a.hit.get(k))} | {_pct(b.hit.get(k))} | {_delta(a.hit.get(k), b.hit.get(k))} |")
    for k in ks:
        rows.append(
            f"| recall@{k} | {_pct(a.recall.get(k))} | {_pct(b.recall.get(k))} | "
            f"{_delta(a.recall.get(k), b.recall.get(k))} |"
        )
    for k in ks:
        rows.append(
            f"| right doc@{k} | {_pct(a.doc.get(k))} | {_pct(b.doc.get(k))} | {_delta(a.doc.get(k), b.doc.get(k))} |"
        )
    rows.append(f"| MRR | {a.mrr:.3f} | {b.mrr:.3f} | {_delta(a.mrr, b.mrr, points=False)} |")
    rows.append(f"| answered nothing (answerable) | {_pct(a.empty)} | {_pct(b.empty)} | {_delta(a.empty, b.empty)} |")
    if a.abstained is not None or b.abstained is not None:
        rows.append(
            f"| abstained (unanswerable) | {_pct(a.abstained)} | {_pct(b.abstained)} | {_delta(a.abstained, b.abstained)} |"
        )
    if a.answer_score is not None or b.answer_score is not None:
        rows.append(
            f"| answer score | {_pct(a.answer_score)} | {_pct(b.answer_score)} | "
            f"{_delta(a.answer_score, b.answer_score)} |"
        )
    return rows


def _group_table(title: str, a: dict[str, Metrics], b: dict[str, Metrics], k: str) -> list[str]:
    lines = [
        f"### By {title} (hit@{k})",
        "",
        f"| {title} | n | baseline | candidate | Δ (pts) |",
        "|---|---:|---:|---:|---:|",
    ]
    for name in sorted(a.keys() | b.keys()):
        ma, mb = a.get(name, Metrics()), b.get(name, Metrics())
        if not (ma.n or mb.n):
            continue
        lines.append(
            f"| {name} | {mb.n or ma.n} | {_pct(ma.hit.get(k))} | {_pct(mb.hit.get(k))} | {_delta(ma.hit.get(k), mb.hit.get(k))} |"
        )
    return [*lines, ""]


def _single_group_table(title: str, groups: dict[str, Metrics], ks: list[str]) -> list[str]:
    lines = [
        f"### By {title}",
        "",
        f"| {title} | n | " + " | ".join(f"hit@{k}" for k in ks) + " | MRR |",
        "|---|---:|" + "---:|" * (len(ks) + 1),
    ]
    for name, m in groups.items():
        if m.n:
            lines.append(f"| {name} | {m.n} | " + " | ".join(_pct(m.hit.get(k)) for k in ks) + f" | {m.mrr:.3f} |")
        elif m.n_unanswerable:
            lines.append(
                f"| {name} (unanswerable) | {m.n_unanswerable} | abstained {_pct(m.abstained)} |" + " |" * len(ks)
            )
    return [*lines, ""]


def _rank(r: CaseResult) -> str:
    return "abstained" if r.abstained else (f"rank {r.first_rank}" if r.first_rank else "missed")


def compare(baseline: Report, candidate: Report, *, k: int = 5) -> Comparison:
    """Markdown describing how ``candidate`` differs from ``baseline``, judged at hit@``k``."""
    ks = [str(x) for x in baseline.run.ks if x in candidate.run.ks] or [str(k)]
    key = str(k) if str(k) in ks else ks[-1]
    lines = [f"# {baseline.run.name} → {candidate.run.name}", ""]
    if baseline.suite_fingerprint != candidate.suite_fingerprint:
        lines += ["> **Different suite versions** — the questions or documents changed between these runs.", ""]
    changes = _setting_changes(baseline, candidate)
    lines += ["## What changed", ""] + ([f"- {c}" for c in changes] or ["- (no setting differs)"]) + [""]
    lines += [
        "## Overall",
        "",
        f"{candidate.overall.n} answerable and {candidate.overall.n_unanswerable} unanswerable questions.",
        "",
        "| metric | baseline | candidate | Δ (pts) |",
        "|---|---:|---:|---:|",
        *_metric_rows(baseline.overall, candidate.overall, ks),
        f"| latency p50 / p95 (ms) | {baseline.latency_ms.get('p50')} / {baseline.latency_ms.get('p95')} | "
        f"{candidate.latency_ms.get('p50')} / {candidate.latency_ms.get('p95')} | |",
        f"| chunks | {sum(i.chunks for i in baseline.ingested)} | {sum(i.chunks for i in candidate.ingested)} | |",
        "",
    ]
    lines += _group_table("kind", baseline.by_kind, candidate.by_kind, key)
    lines += _group_table("format", baseline.by_format, candidate.by_format, key)

    before = {r.id: r for r in baseline.cases}
    regressed: list[str] = []
    improved: list[str] = []
    moved: list[str] = []
    k_int = int(key)
    for now in candidate.cases:
        was = before.get(now.id)
        if was is None:
            continue
        if now.answerable:
            if was.hit(k_int) and not now.hit(k_int):
                regressed.append(now.id)
            elif now.hit(k_int) and not was.hit(k_int):
                improved.append(now.id)
            elif was.first_rank != now.first_rank:
                moved.append(f"- `{now.id}`: {_rank(was)} → {_rank(now)}")
        elif was.abstained != now.abstained:
            (improved if now.abstained else regressed).append(now.id)
        if was.answer and now.answer and was.answer.verdict != now.answer.verdict:
            moved.append(f"- `{now.id}` answer: {was.answer.verdict} → {now.answer.verdict}")

    def describe(ids: list[str]) -> list[str]:
        out = []
        for case_id in ids:
            was, now = before[case_id], next(r for r in candidate.cases if r.id == case_id)
            top = now.retrieved[0] if now.retrieved else None
            got = f" — now top: {top.doc} › {top.section or '-'}" if top else ""
            out.append(f"- `{case_id}` ({now.kind}): {_rank(was)} → {_rank(now)}{got}\n  > {now.question}")
        return out

    lines += [f"## Regressed at hit@{key} ({len(regressed)})", "", *(describe(regressed) or ["None."]), ""]
    lines += [f"## Improved at hit@{key} ({len(improved)})", "", *(describe(improved) or ["None."]), ""]
    if moved:
        lines += ["## Other rank and answer changes", "", *moved, ""]
    hit_delta = (candidate.overall.hit.get(key) or 0.0) - (baseline.overall.hit.get(key) or 0.0)
    return Comparison(text="\n".join(lines), regressed=regressed, improved=improved, hit_delta=hit_delta)


def summary(report: Report) -> str:
    """One run on its own, in Markdown."""
    m = report.overall
    ks = [str(k) for k in report.run.ks]
    e = report.run.embedding
    lines = [
        f"# {report.run.name} — suite {report.suite}",
        "",
        f"{e.get('model_id')} · rerank {report.run.rerank} · {report.run.backend} backend · "
        f"{sum(i.chunks for i in report.ingested)} chunks from {len(report.ingested)} documents · "
        f"git {report.run.git.get('commit')}{' (dirty)' if report.run.git.get('dirty') else ''}",
        "",
        "| metric | " + " | ".join(f"@{k}" for k in ks) + " |",
        "|---|" + "---:|" * len(ks),
        "| hit | " + " | ".join(_pct(m.hit.get(k)) for k in ks) + " |",
        "| recall | " + " | ".join(_pct(m.recall.get(k)) for k in ks) + " |",
        "| right doc | " + " | ".join(_pct(m.doc.get(k)) for k in ks) + " |",
        "",
        f"MRR {m.mrr:.3f} · answered nothing on {_pct(m.empty)} of answerable questions · "
        f"abstained on {_pct(m.abstained)} of unanswerable · latency p50 {report.latency_ms.get('p50')} ms",
    ]
    if m.answer_score is not None:
        lines += ["", f"Answers: score {_pct(m.answer_score)}, fully correct {_pct(m.answers_correct)}"]
    k = "5" if "5" in ks else ks[-1]
    lines += [
        "",
        *_single_group_table("kind", report.by_kind, ks),
        *_single_group_table("format", report.by_format, ks),
    ]
    misses = [r for r in report.cases if r.answerable and not r.hit(int(k))]
    if misses:
        lines += [f"## Missed at hit@{k} ({len(misses)})", ""]
        for r in misses:
            top = r.retrieved[0] if r.retrieved else None
            lines.append(
                f"- `{r.id}` ({r.kind}): {_rank(r)}" + (f" — top: {top.doc} › {top.section or '-'}" if top else "")
            )
    return "\n".join(lines) + "\n"
