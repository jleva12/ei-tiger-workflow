"""Shared pytest configuration.

The coder-team suites run real git. Worktree-based tests inherit the parent repo's
local ``user.*`` config, but distributed-mode tests create fresh *clones*, which do
not — on hosts without a global git identity every commit there would fail. Provide
one via the environment for the whole test session.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _git_identity(monkeypatch):
    monkeypatch.setenv("GIT_AUTHOR_NAME", "etf-tests")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "etf-tests@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "etf-tests")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "etf-tests@example.com")
