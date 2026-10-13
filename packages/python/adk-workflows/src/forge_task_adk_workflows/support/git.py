"""
Git, for the GitHub steps (``graph.factories.github``): cloning a repository
into the run's folder, making a branch there, and committing what changed and
pushing it. Each git command runs as ``git`` with its arguments as they are,
no shell between (``support.commands.run_program``).

- **Only HTTPS.** A repository is ``owner/name`` (on GitHub) or an
  ``https://`` URL, and git may use no other protocol (``GIT_ALLOW_PROTOCOL``):
  a workflow can't read the worker's files (``file://``) or run a program
  (``ext::``) through a remote. Its host is checked as an HTTP step's is
  (``support.http.check_url``): no private network unless the worker allows it.
- **None of the worker's git.** The worker's system and global git settings
  (its credential helpers, aliases, hooks) are left out, and git never asks
  for anything: nobody is there to answer.
- **Signed in by a credential.** One of the organization's credentials signs
  git's requests to the repository's host as it signs an HTTP step's: its
  headers go as ``http.<origin>/.extraHeader``, in git's environment. They're
  never in its arguments (anyone on the machine can list those) or in the
  repository's own settings (which stay in the folder). A token
  (``Bearer``/``token``) goes as basic auth for ``x-access-token``, as GitHub
  takes one for git.
- **Twice is fine.** A run tried again works in the same folder: a clone
  already there of the same repository, and a branch already made, are kept
  and used.
"""

from __future__ import annotations

import asyncio
import base64
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from forge_task_adk_workflows.support.commands import UNASKED, Finished, folder_in, last_line, run_program
from forge_task_adk_workflows.support.errors import StepFailed

#: Where an ``owner/name`` repository is.
GITHUB = "https://github.com"
#: The protocols a repository may use.
PROTOCOLS = ("https",)
#: Who commits when a step doesn't say.
AUTHOR = ("Forge", "forge@localhost")
#: An owner's or a repository's name, as GitHub has them.
_NAME = re.compile(r"[A-Za-z0-9_.-]+")
#: ``owner/name``, maybe as ``github.com/owner/name``.
_SHORT = re.compile(r"(?:github\.com/)?([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?")


@dataclass(frozen=True)
class Repository:
    """A repository: where git finds it, and whose it is."""

    #: What git clones and pushes to.
    url: str
    #: Its scheme, host and port: ``https://github.com``.
    origin: str
    owner: str
    name: str

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"

    @property
    def web_url(self) -> str:
        return f"{self.origin}/{self.owner}/{self.name}"

    @property
    def api_url(self) -> str:
        """GitHub's REST API for it: api.github.com's, or GitHub Enterprise Server's (``/api/v3``)."""
        return "https://api.github.com" if self.origin == GITHUB else f"{self.origin}/api/v3"


def repository(value: str, *, protocols: Sequence[str] = PROTOCOLS) -> Repository:
    """
    :param value: ``owner/name`` on GitHub, or a URL with one of ``protocols``.
    :return: The repository.
    :raises StepFailed: When it's neither, or its URL signs in itself.
    """
    text = value.strip()
    short = _SHORT.fullmatch(text)
    if short is not None:
        owner, name = short.groups()
        return Repository(url=f"{GITHUB}/{owner}/{name}.git", origin=GITHUB, owner=owner, name=name)
    parts = urlsplit(text)
    if parts.scheme not in protocols:
        raise StepFailed(f"Its repository must be owner/name or an https:// URL, not {text[:120]!r}")
    if parts.username or parts.password:
        raise StepFailed("Its repository's URL can't hold a sign-in: pick a credential for it instead")
    if parts.query or parts.fragment:
        raise StepFailed(f"Its repository's URL can't have a query or a fragment: {text[:120]!r}")
    segments = [segment for segment in parts.path.split("/") if segment]
    if len(segments) < 2:
        raise StepFailed(f"Its repository's URL doesn't name an owner and a repository: {text[:120]!r}")
    owner, name = segments[-2], segments[-1].removesuffix(".git")
    if parts.scheme == "https" and not (_NAME.fullmatch(owner) and _NAME.fullmatch(name)):
        raise StepFailed(f"Its repository's owner and name are letters, digits, ., _ and -: {text[:120]!r}")
    url = urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))
    return Repository(url=url, origin=f"{parts.scheme}://{parts.netloc}", owner=owner, name=name)


def signed_in(origin: str, headers: Mapping[str, str]) -> list[tuple[str, str]]:
    """
    :param origin: Where git's requests go: ``https://github.com``.
    :param headers: What a credential has a request there send.
    :return: The git settings that send them, with git's requests there only.
    """
    settings = []
    for name, value in headers.items():
        if name.lower() == "authorization":
            scheme, _, token = value.strip().partition(" ")
            if scheme.lower() in ("bearer", "token") and token.strip():
                pair = f"x-access-token:{token.strip()}".encode()
                value = "Basic " + base64.b64encode(pair).decode()
        settings.append((f"http.{origin}/.extraHeader", f"{name}: {value}"))
    return settings


@dataclass(frozen=True)
class Git:
    """git, as a step runs it: its environment, settings and limits."""

    #: What every command gets of the worker's environment (``command_environment``).
    environment: Mapping[str, str]
    #: How long one command may take.
    seconds: float
    #: The most of a command's output kept.
    max_bytes: int
    #: The protocols a remote may use.
    protocols: Sequence[str] = PROTOCOLS
    #: Settings every command gets: a credential's headers, a step's own.
    settings: tuple[tuple[str, str], ...] = ()
    #: Who commits: a name and an email.
    author: tuple[str, str] = AUTHOR

    def but(self, **changes: Any) -> Git:
        return replace(self, **changes)

    def env(self) -> dict[str, str]:
        """:return: A command's whole environment."""
        env = {
            **self.environment,
            **UNASKED,
            "GIT_ALLOW_PROTOCOL": ":".join(self.protocols),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_AUTHOR_NAME": self.author[0],
            "GIT_AUTHOR_EMAIL": self.author[1],
            "GIT_COMMITTER_NAME": self.author[0],
            "GIT_COMMITTER_EMAIL": self.author[1],
            # Its messages as git writes them, whatever the worker's language.
            "LC_ALL": "C",
        }
        settings = [("init.defaultBranch", "main"), ("advice.detachedHead", "false"), *self.settings]
        env["GIT_CONFIG_COUNT"] = str(len(settings))
        for index, (key, value) in enumerate(settings):
            env[f"GIT_CONFIG_KEY_{index}"] = key
            env[f"GIT_CONFIG_VALUE_{index}"] = value
        return env

    async def run(self, *args: str, cwd: Path) -> Finished:
        """:return: How ``git <args>`` ended, whatever its exit code."""
        return await run_program(
            ("git", *args), cwd=cwd, env=self.env(), seconds=self.seconds, max_bytes=self.max_bytes
        )

    async def __call__(self, *args: str, cwd: Path) -> str:
        """
        :return: What ``git <args>`` wrote to stdout.
        :raises StepFailed: When it fails, with the last thing it said.
        """
        finished = await self.run(*args, cwd=cwd)
        if finished.exit_code != 0:
            said = last_line(finished.stderr) or last_line(finished.stdout)
            raise StepFailed(
                f"git {args[0]} failed" + (f": {said}" if said else f" with {finished.exit_code}"),
                details={"exit_code": finished.exit_code, "stderr": finished.stderr},
            )
        return finished.stdout.strip()


async def branch_name(git: Git, branch: str, folder: Path) -> str:
    """
    :return: ``branch``, when it's a name git takes for a branch.
    :raises StepFailed: When it isn't.
    """
    name = branch.strip()
    if not name:
        raise StepFailed("Its branch is empty")
    finished = await git.run("check-ref-format", "--branch", name, cwd=folder)
    if finished.exit_code != 0 or name.startswith("-"):
        raise StepFailed(f"{name!r} isn't a name git takes for a branch")
    return name


async def current_branch(git: Git, folder: Path) -> str | None:
    """:return: The branch checked out in ``folder``; None when a tag or commit is."""
    finished = await git.run("symbolic-ref", "--quiet", "--short", "HEAD", cwd=folder)
    return finished.stdout.strip() if finished.exit_code == 0 else None


async def a_repository(git: Git, folder: Path) -> Path:
    """
    :return: ``folder``, when it's a git repository's working tree.
    :raises StepFailed: When it isn't.
    """
    finished = await git.run("rev-parse", "--is-inside-work-tree", cwd=folder)
    if finished.exit_code != 0 or finished.stdout.strip() != "true":
        raise StepFailed(f"Its folder, {folder}, isn't a git repository: clone one there first")
    return folder


async def origin_of(git: Git, folder: Path) -> Repository:
    """
    :return: The repository ``folder``'s ``origin`` remote is.
    :raises StepFailed: When it has none, or it isn't one git may reach.
    """
    finished = await git.run("remote", "get-url", "origin", cwd=folder)
    if finished.exit_code != 0:
        raise StepFailed(f"The repository in {folder} has no origin remote to push to")
    return repository(finished.stdout.strip(), protocols=git.protocols)


async def clone(git: Git, repo: Repository, *, workspace: str, folder: str, ref: str, depth: int) -> dict[str, Any]:
    """
    Clone ``repo`` into a folder of the run's folder: ``folder``, or one
    named after it. A clone of it already there is kept as it is.

    :param ref: The branch or tag to check out; empty for its default branch.
    :param depth: How many of the latest commits it gets; 0 for all of them.
    :return: Where it is, and what's checked out.
    :raises StepFailed: When it can't be cloned, or the folder has something else.
    """
    target = await asyncio.to_thread(folder_in, workspace, folder.strip() or repo.name)
    if (target / ".git").exists():
        # A run tried again: its clone is still there.
        there = (await git.run("remote", "get-url", "origin", cwd=target)).stdout.strip()
        if there != repo.url:
            raise StepFailed(f"Its folder, {target}, already has another repository: {there or 'none'}")
        cloned = False
    else:
        if any(target.iterdir()):
            raise StepFailed(f"Its folder, {target}, isn't empty")
        if ref.strip().startswith("-"):
            raise StepFailed(f"{ref.strip()!r} isn't a branch or tag")
        args = ["clone", "--quiet", "--no-recurse-submodules"]
        if depth > 0:
            args += ["--depth", str(depth)]
        if ref.strip():
            args += ["--branch", ref.strip()]
        await git(*args, "--", repo.url, str(target), cwd=Path(workspace))
        cloned = True
    return {
        "folder": str(target),
        "repository": repo.full_name,
        "url": repo.web_url,
        "branch": await current_branch(git, target),
        "commit": await git("rev-parse", "HEAD", cwd=target),
        "cloned": cloned,
    }


async def create_branch(git: Git, folder: Path, branch: str, start: str) -> dict[str, Any]:
    """
    Make a branch in the repository in ``folder`` and check it out; one
    already there is checked out as it is.

    :param start: Where it starts (a branch, tag or commit); empty for what's checked out.
    :return: The branch, and the commit it's at.
    :raises StepFailed: When it can't be made or checked out.
    """
    await a_repository(git, folder)
    name = await branch_name(git, branch, folder)
    exists = (await git.run("show-ref", "--verify", "--quiet", f"refs/heads/{name}", cwd=folder)).exit_code == 0
    if exists:
        await git("switch", "--quiet", name, cwd=folder)
    else:
        args = ["switch", "--quiet", "--create", name]
        if start.strip():
            found = await git.run(
                "rev-parse", "--verify", "--quiet", "--end-of-options", f"{start.strip()}^{{commit}}", cwd=folder
            )
            if found.exit_code != 0:
                raise StepFailed(f"{start.strip()!r} isn't a branch, tag or commit of the repository in {folder}")
            args.append(found.stdout.strip())
        await git(*args, cwd=folder)
    return {
        "folder": str(folder),
        "branch": name,
        "commit": await git("rev-parse", "HEAD", cwd=folder),
        "created": not exists,
    }


async def changed_files(git: Git, folder: Path) -> list[str]:
    """:return: What's staged in ``folder``'s repository, by path."""
    listed = await git("diff", "--cached", "--name-only", "-z", cwd=folder)
    return [path for path in listed.split("\0") if path]


async def commit_and_push(git: Git, folder: Path, branch: str, *, message: str) -> dict[str, Any]:
    """
    Commit everything that changed in the repository in ``folder`` (when
    anything did), and push its branch to ``origin``, setting it to track it.

    :param git: git, signed in for ``origin`` (:func:`origin_of`).
    :param branch: The branch checked out (:func:`current_branch`).
    :param message: The commit's message.
    :return: The branch, the commit pushed, and what it changed.
    :raises StepFailed: When it can't be committed or pushed.
    """
    await git("add", "--all", cwd=folder)
    files = await changed_files(git, folder)
    if files:
        if not message.strip():
            raise StepFailed("Its commit message is empty")
        # Without hooks: nothing the repository brings runs on the worker.
        await git("commit", "--quiet", "--no-verify", "--message", message, cwd=folder)
    await git("push", "--quiet", "--set-upstream", "origin", f"HEAD:refs/heads/{branch}", cwd=folder)
    return {
        "folder": str(folder),
        "branch": branch,
        "commit": await git("rev-parse", "HEAD", cwd=folder),
        "committed": bool(files),
        "files": files,
    }
