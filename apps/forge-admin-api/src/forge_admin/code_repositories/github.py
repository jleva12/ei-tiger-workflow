"""GitHub repository URLs, branches and commits as the code graph worker
accepts them: a port of ``packages/go/code-graph/domain/deployment`` (its
``ParseGitHubURL``, ``CanonicalURL``, ``ValidBranch`` and ``ValidCommit``),
so a repository is named here as the worker names its graph. Both are tested
against ``domain/deployment/testdata/github_urls.json``."""

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

_OWNER = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}")
_NAME = re.compile(r"[A-Za-z0-9_.-]{1,100}")
_COMMIT = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_BRANCH_FORBIDDEN = set("~^:?*[\\")


class InvalidRepository(ValueError):
    """Not a GitHub repository URL the worker accepts; says what is expected."""


@dataclass(frozen=True)
class GitHubRepository:
    """
    A repository on github.com, named as its URL wrote it.

    :ivar owner: The owner (user or organization).
    :ivar name: The repository's name, without ``.git``.
    """

    owner: str
    name: str

    @property
    def url(self) -> str:
        """:return: ``https://github.com/<owner>/<name>``, lowercase: how the
        worker names it."""
        return f"https://github.com/{self.owner}/{self.name}".lower()


def parse_github_url(raw: str) -> GitHubRepository:
    """
    Read a github.com HTTPS repository URL, with an optional ``.git`` suffix
    and trailing slash, and nothing else: no credentials, port, query,
    fragment, escaped path or browser path such as ``/tree/main``.

    :param raw: The URL as written.
    :return: Its owner and name.
    :raises InvalidRepository: It's anything else.
    """
    if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in raw):
        raise InvalidRepository("Use https://github.com/owner/repository")
    parts = urlsplit(raw)
    if (
        parts.scheme != "https"
        or parts.netloc.lower() != "github.com"
        or "?" in raw
        or "#" in raw
        or "%" in parts.path
        or not raw.lower().startswith("https://")
    ):
        raise InvalidRepository("Use https://github.com/owner/repository")
    segments = parts.path.removeprefix("/").removesuffix("/").split("/")
    if len(segments) != 2:
        raise InvalidRepository(
            "Expected a repository URL, https://github.com/owner/repository"
        )
    owner, name = segments[0], segments[1].removesuffix(".git")
    if not _OWNER.fullmatch(owner) or not _NAME.fullmatch(name) or name in (".", ".."):
        raise InvalidRepository("Expected a GitHub owner and repository name")
    return GitHubRepository(owner=owner, name=name)


def valid_branch(branch: str) -> bool:
    """:return: Whether it's a literal Git branch name, never a revision
    expression such as ``main@{1}``."""
    if (
        not branch
        or len(branch.encode()) > 1024
        or branch == "@"
        or branch.startswith("-")
        or branch.endswith(".")
        or ".." in branch
        or "@{" in branch
    ):
        return False
    if any(ord(c) <= 32 or ord(c) == 127 or c in _BRANCH_FORBIDDEN for c in branch):
        return False
    return all(
        part and not part.startswith(".") and not part.endswith(".lock")
        for part in branch.split("/")
    )


def valid_commit(commit: str) -> bool:
    """:return: Whether it's a full commit SHA, lowercase hex (SHA-1 or
    SHA-256)."""
    return _COMMIT.fullmatch(commit) is not None
