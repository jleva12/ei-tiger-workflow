"""Labelled retrieval evaluation: a suite of documents and questions, run
through the real ingest pipeline and knowledge-base search with one set of
settings, scored, and compared with another run. See ``eval/README.md``."""

from forge_task_documents.evaluation.compare import Comparison, compare, summary
from forge_task_documents.evaluation.report import CaseResult, Metrics, Report
from forge_task_documents.evaluation.runner import apply_overrides, check_labels, parse_overrides, run_suite
from forge_task_documents.evaluation.suite import Case, Evidence, EvidenceItem, Suite, load_suite, normalize

__all__ = [
    "Case",
    "CaseResult",
    "Comparison",
    "Evidence",
    "EvidenceItem",
    "Metrics",
    "Report",
    "Suite",
    "apply_overrides",
    "check_labels",
    "compare",
    "load_suite",
    "normalize",
    "parse_overrides",
    "run_suite",
    "summary",
]
