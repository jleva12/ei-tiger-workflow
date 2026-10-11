"""End-to-end grading (``--answers``): the LLM answers each question from the
passages the search found, as an agent would, and grades that answer against
the labelled one. Two LLM calls a question (``HYBRID_LLM__*``)."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import TYPE_CHECKING

from forge_task_documents.evaluation.report import AnswerGrade
from forge_task_documents.evaluation.suite import Case

if TYPE_CHECKING:
    from forge_embeddings.protocols import LLMClient
    from forge_task_documents.retrieval import Passage

NOT_FOUND = "NOT IN DOCUMENTS"

ANSWER_SYSTEM = (
    "You answer questions about a company's documents using only the passages given. "
    "Quote exact figures and names. If the passages don't answer the question, reply exactly: "
    f"{NOT_FOUND}"
)

GRADE_SYSTEM = (
    "You grade an answer to a question against the reference answer. Reply with JSON only: "
    '{"verdict": "correct" | "partial" | "incorrect", "reason": "<one sentence>"}. '
    "Judge only what the question asks: the reference may add context the question didn't ask for, "
    "and leaving that out is fine. "
    "correct: it gives the facts the question asks for, as the reference states them (other wording is fine; "
    "extra correct detail is fine). "
    "partial: some of the facts the question asks for, none contradicted, or the right facts plus a wrong claim. "
    "incorrect: wrong, contradicting, or missing the facts the question asks for. "
    f"When the reference is {NOT_FOUND}, the answer is correct only if it declines to answer."
)

_JSON = re.compile(r"\{.*\}", re.DOTALL)


def _passages_block(passages: Sequence[Passage]) -> str:
    if not passages:
        return "(no passages)"
    return "\n\n".join(
        f"[{i}] document: {p.document} | section: {' > '.join(p.section_path) or '-'}\n{p.text}"
        for i, p in enumerate(passages, 1)
    )


async def grade_answer(llm: LLMClient, case: Case, passages: Sequence[Passage]) -> AnswerGrade:
    answer = (
        await llm.complete(
            system=ANSWER_SYSTEM,
            user=f"Question: {case.question}\n\nPassages:\n{_passages_block(passages)}",
            max_tokens=400,
        )
    ).strip()
    reference = case.answer if case.answerable else NOT_FOUND
    reply = await llm.complete(
        system=GRADE_SYSTEM,
        user=f"Question: {case.question}\nReference answer: {reference}\nAnswer to grade: {answer}",
        max_tokens=200,
    )
    try:
        found = _JSON.search(reply)
        graded = json.loads(found.group(0) if found else reply)
        verdict = str(graded.get("verdict", "")).lower()
        reason = str(graded.get("reason", ""))
    except (json.JSONDecodeError, AttributeError):
        verdict, reason = "incorrect", f"unparseable grade: {reply[:200]}"
    if verdict not in ("correct", "partial", "incorrect"):
        verdict = "incorrect"
    return AnswerGrade(answer=answer, verdict=verdict, reason=reason)
