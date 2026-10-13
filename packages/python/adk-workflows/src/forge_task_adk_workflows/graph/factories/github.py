"""
The GitHub steps. Clone repository, Create branch and Commit and push work
with git in the run's folder (``support.git``); Open pull request asks
GitHub's REST API, as an HTTP step asks one (``actions.send``).

Each is signed in by one of the organization's credentials, as an HTTP step
is: one for ``https://github.com`` (git's requests) and
``https://api.github.com`` (the API's), holding a token. Each takes its
Success way with what it did, or its Error way with why not. A run tried
again goes on from what it did already: the clone and the branch are kept,
a commit already pushed pushes nothing, and a pull request already open for
the branch is the one it hands on.
"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlencode

from google.adk import Context, Event
from google.adk.workflow import FunctionNode

from forge_task_adk_workflows.graph.data import ERROR_KEY
from forge_task_adk_workflows.graph.errors import RunFailed
from forge_task_adk_workflows.graph.factories.actions import CredentialRefused, send, signer
from forge_task_adk_workflows.graph.factories.base import BuildContext, failed, label_of, settings_of, text
from forge_task_adk_workflows.graph.names import adk_name
from forge_task_adk_workflows.graph.services import RunServices
from forge_task_adk_workflows.support.commands import folder_in
from forge_task_adk_workflows.support.errors import StepFailed
from forge_task_adk_workflows.support.git import (
    AUTHOR,
    Git,
    Repository,
    a_repository,
    clone,
    commit_and_push,
    create_branch,
    current_branch,
    origin_of,
    repository,
    signed_in,
)
from forge_task_adk_workflows.support.http import check_url
from forge_task_adk_workflows.support.step_settings import (
    GitBranchConfig,
    GitCloneConfig,
    GithubPullRequestConfig,
    GitPushConfig,
)

#: What a step does with the run's data and its folder: what it hands on.
Act = Callable[[dict[str, Any], str | None], Awaitable[dict[str, Any]]]
#: What GitHub's API is asked with.
API_HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}


def _step(node: dict[str, Any], ctx: BuildContext, act: Act, *, seconds: float, in_folder: bool) -> FunctionNode:
    """A GitHub step's node: ``act`` within ``seconds``, by its Success way or its Error way."""
    has_error_way = "error" in ctx.connected.get(node["id"], frozenset())

    async def run(adk: Context, node_input: Any) -> Event:
        workspace = ctx.services.workspace
        if in_folder and workspace is None:
            raise RunFailed(
                f"{label_of(node)} failed: the run has no folder to work in: "
                "turn on Allow file system on the workflow's start",
                step=node["id"],
            )
        try:
            try:
                async with asyncio.timeout(seconds):
                    output = await act(ctx.data(adk, node_input), workspace)
            except TimeoutError:
                raise StepFailed(f"It didn't finish in {seconds:g} seconds") from None
        except StepFailed as error:
            if not has_error_way:
                raise failed(node, error) from None
            return Event(route="error", custom_metadata={ERROR_KEY: error.as_error()})  # type: ignore[call-arg]  # ADK's shorthand for actions.route
        return Event(output=output, route="success")  # type: ignore[call-arg]  # ADK's shorthand for actions.route

    return FunctionNode(name=adk_name(text(node.get("name"))), func=run)


def _git(services: RunServices, seconds: float, author: tuple[str, str] = AUTHOR) -> Git:
    return Git(
        environment=services.command_environment,
        seconds=seconds,
        max_bytes=services.max_command_output_bytes,
        protocols=services.git_protocols,
        author=author,
    )


async def _signed(git: Git, services: RunServices, credential: str, repo: Repository) -> Git:
    """git, its requests to ``repo`` checked (as an HTTP step's are) and signed in by ``credential``."""
    if repo.url.startswith("https://"):
        await check_url(repo.url, allow_private=services.allow_private, allowed_hosts=list(services.allowed_hosts))
    headers, params = await signer(services, credential)(repo.url, True)
    if params:
        raise CredentialRefused(
            "Its credential sends its key in the URL's query, which git can't: use one with a token"
        )
    return git.but(settings=tuple(signed_in(repo.origin, headers)))


def git_clone(node: dict[str, Any], ctx: BuildContext) -> FunctionNode:
    s = settings_of(node, GitCloneConfig)
    services = ctx.services
    evaluator = services.evaluator

    async def act(data: dict[str, Any], workspace: str | None) -> dict[str, Any]:
        assert workspace is not None
        found = await evaluator.render(s.repository, data, what="Its repository")
        repo = repository(found, protocols=services.git_protocols)
        ref = await evaluator.render(s.ref, data, what="Its branch or tag")
        folder = await evaluator.render(s.folder, data, what="Its folder")
        git = await _signed(_git(services, s.timeout_seconds), services, s.credential, repo)
        return await clone(git, repo, workspace=workspace, folder=folder, ref=ref, depth=s.depth)

    return _step(node, ctx, act, seconds=s.timeout_seconds, in_folder=True)


def git_branch(node: dict[str, Any], ctx: BuildContext) -> FunctionNode:
    s = settings_of(node, GitBranchConfig)
    services = ctx.services
    evaluator = services.evaluator

    async def act(data: dict[str, Any], workspace: str | None) -> dict[str, Any]:
        assert workspace is not None
        where = await evaluator.render(s.folder, data, what="Its folder")
        folder = await asyncio.to_thread(folder_in, workspace, where)
        branch = await evaluator.render(s.branch, data, what="Its branch")
        start = await evaluator.render(s.start, data, what="Where it starts")
        return await create_branch(_git(services, s.timeout_seconds), folder, branch, start)

    return _step(node, ctx, act, seconds=s.timeout_seconds, in_folder=True)


def git_push(node: dict[str, Any], ctx: BuildContext) -> FunctionNode:
    s = settings_of(node, GitPushConfig)
    services = ctx.services
    evaluator = services.evaluator

    async def act(data: dict[str, Any], workspace: str | None) -> dict[str, Any]:
        assert workspace is not None
        where = await evaluator.render(s.folder, data, what="Its folder")
        folder = await asyncio.to_thread(folder_in, workspace, where)
        name = (await evaluator.render(s.author_name, data, what="Its author's name")).strip()
        email = (await evaluator.render(s.author_email, data, what="Its author's email")).strip()
        git = _git(services, s.timeout_seconds, (name or AUTHOR[0], email or AUTHOR[1]))
        await a_repository(git, folder)
        branch = await current_branch(git, folder)
        if branch is None:
            raise StepFailed(
                f"The repository in {folder} isn't on a branch (a tag or commit is checked out): "
                "make one with a Create branch step first"
            )
        repo = await origin_of(git, folder)
        git = await _signed(git, services, s.credential, repo)
        message = await evaluator.render(s.message, data, what="Its message")
        return {**await commit_and_push(git, folder, branch, message=message), "repository": repo.full_name}

    return _step(node, ctx, act, seconds=s.timeout_seconds, in_folder=True)


def github_pull_request(node: dict[str, Any], ctx: BuildContext) -> FunctionNode:
    s = settings_of(node, GithubPullRequestConfig)
    services = ctx.services
    evaluator = services.evaluator
    sign = signer(services, s.credential)

    async def api(method: str, url: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        sent = {"json": body} if body is not None else {}
        return await send(services, method, url, dict(API_HEADERS), sent, seconds=s.timeout_seconds, sign=sign)

    async def act(data: dict[str, Any], _workspace: str | None) -> dict[str, Any]:
        found = await evaluator.render(s.repository, data, what="Its repository")
        repo = repository(found)
        head = (await evaluator.render(s.head, data, what="Its branch")).strip()
        base = (await evaluator.render(s.base, data, what="The branch it merges into")).strip()
        title = (await evaluator.render(s.title, data, what="Its title")).strip()
        body = await evaluator.render(s.body, data, what="Its description")
        if not head:
            raise StepFailed("Its branch, the one it merges, is empty")
        if not title:
            raise StepFailed("Its title is empty")
        at = f"{repo.api_url}/repos/{repo.owner}/{repo.name}"
        if not base:
            base = text(_answer(await api("GET", at)).get("default_branch"))
            if not base:
                raise StepFailed(f"GitHub gave no default branch for {repo.full_name}")
        made = await api(
            "POST", f"{at}/pulls", {"title": title, "head": head, "base": base, "body": body, "draft": s.draft}
        )
        if made["status"] == 201 and isinstance(made["body"], dict):
            return _pull_request(made["body"], created=True)
        if made["status"] == 422 and _already_open(made["body"]):
            # A run tried again: its pull request is open already.
            owned = head if ":" in head else f"{repo.owner}:{head}"
            query = urlencode({"head": owned, "base": base, "state": "open"})
            listed = (await api("GET", f"{at}/pulls?{query}"))["body"]
            if isinstance(listed, list) and listed and isinstance(listed[0], dict):
                return _pull_request(listed[0], created=False)
        raise _refused(made)

    return _step(node, ctx, act, seconds=s.timeout_seconds, in_folder=False)


def _answer(response: dict[str, Any]) -> dict[str, Any]:
    """:return: An API answer's body, when it's a success; :raises StepFailed: otherwise."""
    if 200 <= response["status"] < 300 and isinstance(response["body"], dict):
        return response["body"]
    raise _refused(response)


def _object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _messages(body: Any) -> list[str]:
    """:return: What a GitHub error says: its message, and each of its errors'."""
    found = _object(body)
    errors = found.get("errors")
    each = [text(_object(error).get("message")) for error in errors] if isinstance(errors, list) else []
    return [said for said in (text(found.get("message")), *each) if said]


def _refused(response: dict[str, Any]) -> StepFailed:
    """:return: What GitHub's answer says went wrong."""
    reason = "; ".join(_messages(response["body"]))
    return StepFailed(
        f"GitHub answered {response['status']}" + (f": {reason}" if reason else ""),
        status=response["status"],
        details={"body": response["body"]},
    )


def _already_open(body: Any) -> bool:
    """:return: Whether GitHub refused a pull request because one is open for its branch already."""
    return any("already exists" in said for said in _messages(body))


def _pull_request(found: dict[str, Any], *, created: bool) -> dict[str, Any]:
    """:return: What a step hands on of a pull request, as GitHub's API has it."""
    head = _object(found.get("head"))
    base = _object(found.get("base"))
    return {
        "number": found.get("number"),
        "url": text(found.get("html_url")),
        "title": text(found.get("title")),
        "state": text(found.get("state")),
        "draft": found.get("draft") is True,
        "head": text(head.get("ref")),
        "base": text(base.get("ref")),
        "created": created,
    }
