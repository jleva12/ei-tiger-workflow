"""Repositories are named as the code graph worker names them: the same
cases its Go functions are tested against (github_urls.json)."""

import json
from pathlib import Path

import pytest

from forge_admin.code_repositories.github import (
    InvalidRepository,
    parse_github_url,
    valid_branch,
    valid_commit,
)

VECTORS = (
    Path(__file__).resolve().parents[4]
    / "packages/go/code-graph/domain/deployment/testdata/github_urls.json"
)


@pytest.fixture(scope="module")
def vectors() -> dict:
    if not VECTORS.exists():
        pytest.skip("the Go module's test data is not here")
    return json.loads(VECTORS.read_text())


def test_urls_are_read_as_the_worker_reads_them(vectors: dict) -> None:
    for case in vectors["urls"]:
        if case.get("canonical"):
            assert parse_github_url(case["url"]).url == case["canonical"], case
        else:
            with pytest.raises(InvalidRepository):
                parse_github_url(case["url"])
                pytest.fail(f"accepted {case['url']!r}")


def test_branches_and_commits(vectors: dict) -> None:
    assert all(valid_branch(b) for b in vectors["branches"]["valid"])
    assert not any(valid_branch(b) for b in vectors["branches"]["invalid"])
    assert all(valid_commit(c) for c in vectors["commits"]["valid"])
    assert not any(valid_commit(c) for c in vectors["commits"]["invalid"])


def test_the_owner_and_name_are_kept_as_written() -> None:
    repository = parse_github_url("https://github.com/Acme/Widgets.git")
    assert (repository.owner, repository.name) == ("Acme", "Widgets")
