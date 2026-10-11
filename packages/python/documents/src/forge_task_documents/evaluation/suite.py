"""A labelled evaluation suite: documents of several formats and the questions
asked of them, each labelled with its answer and the evidence that answers it.

Labels name text, never chunk ids, so one suite scores any chunking, any
embedding model and any search settings::

    # suite.yaml
    name: core
    documents:
      - id: claims-manual
        file: corpus/claims_operations_manual.md
    questions: questions.yaml

    # questions.yaml
    - id: cm-na-senior-home
      question: What can a Senior Adjuster in North America approve without review on a Home claim?
      answer: Up to 25,000 USD.
      kind: hierarchy
      evidence:
        - doc: claims-manual
          contains: ["North America", "Senior Adjuster", "Home", "up to 25,000 USD"]

A passage satisfies an evidence item when it comes from that document and
its section and text contain every string (case, whitespace, quotes and
dashes normalized). Every item must be satisfied for the question to count
as answered; ``any:`` lists alternatives (a fact stated in two places). A
question with ``answerable: false`` and no evidence is one the documents
don't answer: the search should find nothing.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

_SPACE = re.compile(r"\s+")
_FOLD = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-", "‑": "-", " ": " "})


def normalize(text: str) -> str:
    """Case, width, quotes, dashes and runs of whitespace folded, for matching labels to passages."""
    return _SPACE.sub(" ", unicodedata.normalize("NFKC", text).translate(_FOLD)).strip().casefold()


class Evidence(BaseModel):
    """Text one passage of one document must hold."""

    doc: str
    contains: list[str] = Field(min_length=1)

    @field_validator("contains", mode="before")
    @classmethod
    def _texts(cls, value: object) -> object:
        return [str(v) for v in value] if isinstance(value, list) else value

    def satisfied_by(self, doc_id: str, text: str) -> bool:
        if doc_id != self.doc:
            return False
        haystack = normalize(text)
        return all(normalize(needle) in haystack for needle in self.contains)


class EvidenceItem(BaseModel):
    """One piece of evidence the answer needs: a single passage's, or any of several alternatives."""

    any: list[Evidence] = Field(default_factory=list)
    doc: str | None = None
    contains: list[str] = Field(default_factory=list)

    @field_validator("contains", mode="before")
    @classmethod
    def _texts(cls, value: object) -> object:
        return [str(v) for v in value] if isinstance(value, list) else value

    @model_validator(mode="after")
    def _one_form(self) -> EvidenceItem:
        if self.any and (self.doc or self.contains):
            raise ValueError("evidence is either doc + contains, or any: [...]")
        if not self.any and not (self.doc and self.contains):
            raise ValueError("evidence needs doc and contains (or any: [...])")
        return self

    @property
    def options(self) -> list[Evidence]:
        return self.any or [Evidence(doc=self.doc or "", contains=self.contains)]

    @property
    def docs(self) -> set[str]:
        return {option.doc for option in self.options}

    def satisfied_by(self, doc_id: str, text: str) -> bool:
        return any(option.satisfied_by(doc_id, text) for option in self.options)


class Case(BaseModel):
    """One labelled question."""

    id: str
    question: str
    answer: str
    kind: str = "fact"
    answerable: bool = True
    evidence: list[EvidenceItem] = Field(default_factory=list)
    notes: str | None = None

    @field_validator("question", "answer", mode="before")
    @classmethod
    def _text(cls, value: object) -> object:
        # YAML reads an unquoted answer like ``44.`` or ``2026-08-04`` as a number or date.
        return value if isinstance(value, str) or value is None else str(value)

    @model_validator(mode="after")
    def _evidence_matches_answerable(self) -> Case:
        if self.answerable and not self.evidence:
            raise ValueError(f"{self.id}: an answerable question needs evidence")
        if not self.answerable and self.evidence:
            raise ValueError(f"{self.id}: an unanswerable question has no evidence")
        return self

    @property
    def docs(self) -> set[str]:
        return {doc for item in self.evidence for doc in item.docs}


class SuiteDocument(BaseModel):
    id: str
    file: str
    about: str | None = None


class Suite(BaseModel):
    name: str
    description: str = ""
    documents: list[SuiteDocument]
    cases: list[Case]
    root: Path
    fingerprint: str

    @property
    def documents_by_id(self) -> dict[str, SuiteDocument]:
        return {d.id: d for d in self.documents}

    def path(self, document: SuiteDocument) -> Path:
        return self.root / document.file

    def format_of(self, doc_id: str) -> str:
        return Path(self.documents_by_id[doc_id].file).suffix.lstrip(".").lower()

    def formats(self, case: Case) -> list[str]:
        return sorted({self.format_of(doc) for doc in case.docs})


def load_suite(path: str | Path) -> Suite:
    """Read a suite: ``path`` is its directory (holding ``suite.yaml``) or that file."""
    path = Path(path)
    spec_path = path / "suite.yaml" if path.is_dir() else path
    root = spec_path.parent
    spec: dict[str, Any] = yaml.safe_load(spec_path.read_text())
    questions = spec.pop("questions", [])
    question_files: list[Path] = []
    if isinstance(questions, str):
        questions = [questions]
    cases: list[Any] = []
    for entry in questions:
        if isinstance(entry, str):
            question_files.append(root / entry)
            cases.extend(yaml.safe_load((root / entry).read_text()) or [])
        else:
            cases.append(entry)
    documents = [SuiteDocument.model_validate(d) for d in spec.pop("documents", [])]
    suite = Suite(
        name=spec.get("name") or root.name,
        description=spec.get("description", ""),
        documents=documents,
        cases=[Case.model_validate(c) for c in cases],
        root=root,
        fingerprint=_fingerprint([spec_path, *question_files, *(root / d.file for d in documents)]),
    )
    problems = check_suite(suite)
    if problems:
        raise ValueError("suite is inconsistent:\n  " + "\n  ".join(problems))
    return suite


def check_suite(suite: Suite) -> list[str]:
    """Structural problems: duplicate ids, evidence naming unknown documents, missing files."""
    problems: list[str] = []
    for name, ids in (("document", [d.id for d in suite.documents]), ("question", [c.id for c in suite.cases])):
        problems += [f"duplicate {name} id {i!r}" for i, n in Counter(ids).items() if n > 1]
    known = set(suite.documents_by_id)
    for case in suite.cases:
        problems += [f"{case.id}: evidence names unknown document {doc!r}" for doc in sorted(case.docs - known)]
    problems += [f"missing file {d.file}" for d in suite.documents if not suite.path(d).is_file()]
    return problems


def _fingerprint(paths: Iterable[Path]) -> str:
    """Changes when any of the suite's files does: two reports compare only on the same suite."""
    digest = hashlib.sha256()
    for path in sorted(paths):
        digest.update(path.name.encode())
        digest.update(path.read_bytes() if path.is_file() else b"")
    return digest.hexdigest()[:16]
