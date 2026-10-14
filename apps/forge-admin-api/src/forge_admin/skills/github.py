"""Skills imported from a GitHub repository: every skill (a folder with a
``SKILL.md``) in a folder of it, at a branch, tag or commit.

The repository is named by its URL: ``https://github.com/owner/repo``, or a
folder's as GitHub shows it (``…/tree/main/skills``), or a ``SKILL.md``'s
(``…/blob/main/skills/pdf/SKILL.md``), or ``owner/repo``; a path and a ref
given beside it say which folder and which branch, tag or commit. The ref is
pinned to its commit as it's read, so a skill says exactly what it came from
and can be pulled again.

A public repository is read without signing in: the GitHub API for the tree
(three calls or so) and ``raw.githubusercontent.com`` for the files. A
private one is read with one of the organization's credentials that may be
sent to ``https://api.github.com``, through the API alone. Only the files in
skills' folders are fetched, each no larger than a skill's file may be.
"""

import asyncio
import posixpath
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlsplit

import httpx2 as httpx
from forge_agent_runtime.skills import (
    MAX_ARCHIVE_BYTES,
    MAX_FILE_BYTES,
    FoundSkills,
    find_skills,
    ignored,
)

API = "https://api.github.com"
RAW = "https://raw.githubusercontent.com"
API_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}
#: The most files an import fetches: those in the skills' folders.
MAX_FILES = 2_000
#: How many files are fetched at once.
CONCURRENCY = 8
#: Seconds one request may take.
TIMEOUT = 30.0

_OWNER = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}")
_NAME = re.compile(r"[A-Za-z0-9_.-]{1,100}")
_REF = re.compile(r"[A-Za-z0-9._/@+-]{1,255}")

#: Signs a request to a GitHub URL: the headers a credential sends there.
Signer = Callable[[str], Awaitable[Mapping[str, str]]]


class GitHubError(Exception):
    """
    The repository, folder or ref can't be read, and why.

    :ivar status: What the route answers: 422 for what was asked, 404 for
        what isn't there (or can't be seen), 429 for GitHub's rate limit,
        502 for GitHub failing.
    """

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class GitHubLocation:
    """
    A folder of a repository, at a ref.

    :ivar choices: Where the ref ends and the path begins, to try in turn:
        a folder URL can't say (``tree/release/v2/skills``); empty to use
        ``ref`` and ``path``.
    """

    owner: str
    name: str
    ref: str | None
    path: str
    choices: tuple[tuple[str, str], ...] = ()

    @property
    def repository(self) -> str:
        return f"https://github.com/{self.owner}/{self.name}"


@dataclass
class Imported:
    """
    What a folder of a repository holds.

    :ivar found: Its skills, each by its folder's path in the repository.
    """

    repository: str
    ref: str
    commit: str
    path: str
    found: FoundSkills = field(default_factory=FoundSkills)

    def source(self, folder: str, credential: str | None) -> dict[str, Any]:
        """:return: Where a skill in it came from, as a skill keeps it."""
        return {
            "kind": "github",
            "repository": self.repository,
            "ref": self.ref,
            "commit": self.commit,
            "path": folder,
            **({"credential": credential} if credential else {}),
        }


def _clean(path: str) -> str:
    """:raises GitHubError: It isn't a path in the repository."""
    text = path.strip().strip("/")
    if not text:
        return ""
    parts = text.split("/")
    if any(part in ("", ".", "..") for part in parts) or any(ord(c) < 32 for c in text):
        raise GitHubError(422, f"{path!r} isn't a folder of the repository")
    # A SKILL.md's URL means its folder.
    if parts[-1].lower() == "skill.md":
        parts = parts[:-1]
    return "/".join(parts)


def parse_location(url: str, *, path: str = "", ref: str = "") -> GitHubLocation:
    """
    :param url: The repository's URL, a folder's or a SKILL.md's, or ``owner/repo``.
    :param path: The folder to read; the URL's (or the whole repository) when empty.
    :param ref: The branch, tag or commit; the URL's (or the default branch) when empty.
    :raises GitHubError: It isn't a GitHub repository's URL.
    """
    text = url.strip()
    if re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]+", text):
        text = f"https://github.com/{text}"
    parts = urlsplit(text)
    if parts.scheme not in ("https", "http") or parts.netloc.lower() not in (
        "github.com",
        "www.github.com",
    ):
        raise GitHubError(
            422, "Use a GitHub repository's URL: https://github.com/owner/repository"
        )
    if parts.query or parts.fragment or parts.username or "%" in parts.path:
        raise GitHubError(
            422,
            "Use the repository's URL as GitHub shows it, without ?, # or a sign-in",
        )
    segments = [s for s in parts.path.split("/") if s]
    if len(segments) < 2:
        raise GitHubError(
            422, "Expected a repository: https://github.com/owner/repository"
        )
    owner, name = segments[0], segments[1].removesuffix(".git")
    if not _OWNER.fullmatch(owner) or not _NAME.fullmatch(name) or name in (".", ".."):
        raise GitHubError(422, "Expected a GitHub owner and repository name")
    rest = segments[2:]
    choices: list[tuple[str, str]] = []
    if rest:
        if rest[0] not in ("tree", "blob") or len(rest) < 2:
            raise GitHubError(
                422, "Use a repository's URL, or a folder's: …/tree/<branch>/<folder>"
            )
        after = rest[1:]
        # The ref may have slashes: release/v2. The longest ref comes last.
        for cut in range(1, min(len(after), 4) + 1):
            choices.append(("/".join(after[:cut]), "/".join(after[cut:])))
    explicit_ref = ref.strip()
    if explicit_ref and not _REF.fullmatch(explicit_ref):
        raise GitHubError(422, f"{ref!r} isn't a branch, tag or commit")
    if path.strip():
        # Said beside the URL: it wins over the URL's, and so does a ref.
        return GitHubLocation(owner, name, explicit_ref or None, _clean(path))
    if explicit_ref or not choices:
        return GitHubLocation(
            owner,
            name,
            explicit_ref or None,
            _clean(choices[0][1]) if choices else "",
        )
    return GitHubLocation(
        owner,
        name,
        None,
        "",
        tuple((r, _clean(p)) for r, p in choices),
    )


class GitHubSkills:
    """
    Reads skills from GitHub.

    :param client: What it sends with (a test's answers from a mock transport).
    :param sign: Signs requests to ``api.github.com`` with a credential; None
        to read a public repository.
    """

    def __init__(self, client: httpx.AsyncClient, sign: Signer | None = None) -> None:
        self.client = client
        self.sign = sign

    async def _get(self, url: str, *, raw: bool = False) -> httpx.Response:
        headers = dict(API_HEADERS)
        if raw:
            headers["Accept"] = "application/vnd.github.raw"
        if self.sign is not None and url.startswith(API):
            headers.update(await self.sign(url))
        try:
            return await self.client.get(
                url, headers=headers, timeout=TIMEOUT, follow_redirects=True
            )
        except httpx.HTTPError as error:
            raise GitHubError(
                502, f"GitHub didn't answer: {type(error).__name__}"
            ) from None

    async def _json(self, url: str, location: GitHubLocation, missing: str) -> Any:
        response = await self._get(url)
        if response.status_code == 200:
            return response.json()
        raise self._refused(response, location, missing)

    def _refused(
        self, response: httpx.Response, location: GitHubLocation, missing: str
    ) -> GitHubError:
        if response.status_code in (403, 429) and (
            response.headers.get("x-ratelimit-remaining") == "0"
            or "rate limit" in response.text.lower()
        ):
            return GitHubError(
                429,
                "GitHub's rate limit for reading without signing in is used up: "
                "pick a credential that signs in to GitHub, or try again later",
            )
        if response.status_code in (401, 403):
            return GitHubError(
                404 if self.sign is None else 403,
                f"GitHub refused to show {location.repository}"
                + (
                    ": the credential may not read it"
                    if self.sign
                    else ": if it's private, pick a credential that signs in to GitHub"
                ),
            )
        if response.status_code in (404, 422):
            return GitHubError(404, missing)
        return GitHubError(
            502, f"GitHub answered {response.status_code} for {location.repository}"
        )

    def _repo(self, location: GitHubLocation) -> str:
        return f"{API}/repos/{quote(location.owner)}/{quote(location.name)}"

    async def _commit(
        self, location: GitHubLocation, ref: str
    ) -> tuple[str, str] | None:
        """:return: The ref's commit and its root tree; None when there's no such ref."""
        response = await self._get(
            f"{self._repo(location)}/commits/{quote(ref, safe='')}"
        )
        if response.status_code in (404, 422):
            return None
        if response.status_code != 200:
            raise self._refused(response, location, "")
        body = response.json()
        return str(body["sha"]), str(body["commit"]["tree"]["sha"])

    async def _pinned(self, location: GitHubLocation) -> tuple[str, str, str, str]:
        """:return: The ref, the folder, the commit and the commit's root tree."""
        missing = f"GitHub has no repository {location.owner}/{location.name}" + (
            ""
            if self.sign
            else ", or it's private: pick a credential that signs in to GitHub"
        )
        repository = await self._json(self._repo(location), location, missing)
        choices = list(location.choices) or [
            (
                location.ref or str(repository.get("default_branch") or "main"),
                location.path,
            )
        ]
        for ref, path in choices:
            found = await self._commit(location, ref)
            if found is not None:
                return ref, path, *found
        raise GitHubError(
            404, f"{location.repository} has no branch, tag or commit {choices[0][0]!r}"
        )

    async def _folder(
        self, location: GitHubLocation, tree: str, path: str, ref: str
    ) -> str:
        """:return: The folder's tree, found segment by segment from the root's."""
        for segment in [s for s in path.split("/") if s]:
            listing = await self._json(
                f"{self._repo(location)}/git/trees/{tree}", location, ""
            )
            entry = next(
                (
                    e
                    for e in listing.get("tree") or []
                    if e.get("path") == segment and e.get("type") == "tree"
                ),
                None,
            )
            if entry is None:
                raise GitHubError(
                    404, f"{location.repository} has no folder {path} at {ref}"
                )
            tree = str(entry["sha"])
        return tree

    async def read(self, location: GitHubLocation) -> Imported:
        """
        :return: The skills in the folder, at the ref's commit.
        :raises GitHubError: It can't be read, saying why.
        """
        ref, path, commit, root = await self._pinned(location)
        tree = await self._folder(location, root, path, ref)
        listing = await self._json(
            f"{self._repo(location)}/git/trees/{tree}?recursive=1", location, ""
        )
        if listing.get("truncated"):
            raise GitHubError(
                422,
                "The folder has too many files to read whole: give the path of "
                "the folder its skills are in",
            )
        blobs = {
            str(e["path"]): e
            for e in listing.get("tree") or []
            if e.get("type") == "blob" and not ignored(str(e.get("path")))
        }
        roots = {
            posixpath.dirname(p)
            for p in blobs
            if posixpath.basename(p).lower() == "skill.md"
        }
        imported = Imported(location.repository, ref, commit, path)
        if not roots:
            return imported

        def in_a_skill(p: str) -> bool:
            return any(root == "" or p.startswith(f"{root}/") for root in roots)

        wanted = {p: e for p, e in blobs.items() if in_a_skill(p)}
        large = [
            p for p, e in wanted.items() if int(e.get("size") or 0) > MAX_FILE_BYTES
        ]
        for p in large:
            del wanted[p]
        if len(wanted) > MAX_FILES:
            raise GitHubError(
                422,
                f"Its skills have {len(wanted)} files; an import reads {MAX_FILES} at "
                "most: give the path of one skill's folder",
            )
        if sum(int(e.get("size") or 0) for e in wanted.values()) > MAX_ARCHIVE_BYTES:
            raise GitHubError(
                422,
                f"Its skills are larger than {MAX_ARCHIVE_BYTES // 1_000_000} MB "
                "together: give the path of one skill's folder",
            )
        gate = asyncio.Semaphore(CONCURRENCY)

        async def fetch(p: str, entry: Mapping[str, Any]) -> tuple[str, bytes]:
            full = f"{path}/{p}" if path else p
            if self.sign is not None:
                url = f"{self._repo(location)}/git/blobs/{entry['sha']}"
            else:
                url = (
                    f"{RAW}/{quote(location.owner)}/{quote(location.name)}/{commit}/"
                    + quote(full)
                )
            async with gate:
                response = await self._get(url, raw=True)
            if response.status_code != 200:
                raise self._refused(response, location, f"GitHub has no file {full}")
            return p, response.content

        files = dict(await asyncio.gather(*(fetch(p, e) for p, e in wanted.items())))
        found = find_skills(files)
        prefix = f"{path}/" if path else ""
        imported.found = FoundSkills(
            skills=[
                ((prefix + root).strip("/"), skill) for root, skill in found.skills
            ],
            problems=[
                type(problem)(prefix + problem.path, problem.message)
                for problem in found.problems
            ],
            skipped=[prefix + p for p in found.skipped]
            + [
                f"{prefix}{p} (larger than {MAX_FILE_BYTES // 1_000_000} MB)"
                for p in large
            ],
        )
        return imported
