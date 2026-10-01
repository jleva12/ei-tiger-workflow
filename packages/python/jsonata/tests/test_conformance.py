"""Run every pinned upstream case, without generated files or a Git submodule."""

import hashlib
import json
from pathlib import Path

import pytest

from tests.upstream.jsonata_test import TestJsonata as UpstreamRunner

PACKAGE = Path(__file__).resolve().parents[1]
SUITE = PACKAGE / "tests/conformance"


def collect_cases():
    manifest = json.loads((PACKAGE / "upstream-manifest.json").read_text())
    actual_files = {path.relative_to(PACKAGE).as_posix() for path in SUITE.rglob("*") if path.is_file()} | {
        "tests/upstream/test-overrides.json"
    }
    assert actual_files == set(manifest["files"]), "Conformance fixture inventory changed"
    for name, digest in manifest["files"].items():
        assert hashlib.sha256((PACKAGE / name).read_bytes()).hexdigest() == digest, name

    cases = []
    groups = sorted(path for path in (SUITE / "groups").iterdir() if path.is_dir())
    assert len(groups) == manifest["conformance_groups"]
    for group in groups:
        for path in sorted(group.glob("*.json")):
            document = json.loads(path.read_text(encoding="utf-8"))
            # The upstream runner splits expression-file references on '/'.
            name = path.as_posix()
            case_id = path.relative_to(SUITE / "groups").as_posix()
            if isinstance(document, list):
                for index, case in enumerate(document):
                    cases.append(pytest.param(f"{name}_{index}", case, id=f"{case_id}_{index}"))
            else:
                cases.append(pytest.param(name, document, id=case_id))
    assert len(cases) == manifest["conformance_cases"]
    return cases


@pytest.mark.parametrize(("name", "case"), collect_cases())
def test_upstream_conformance(name, case):
    assert UpstreamRunner().run_test_case(name, case), name
