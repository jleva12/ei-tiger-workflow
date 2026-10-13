"""
The GitHub steps: Clone repository, Create branch and Commit and push run
git in the run's folder (against a local bare repository standing in for
GitHub), and Open pull request asks GitHub's API (a fake one).
"""

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import httpx
import pytest
from forge_agent_runtime import RequestAuth

from forge_task_adk_workflows.support.errors import StepFailed
from forge_task_adk_workflows.support.git import Git, repository, signed_in

from .test_commands import FILE_SYSTEM_START, bash, in_folder, workflow
from .test_graph import START_NODE, agent, end, node, ran, refused, run, services

#: git as the tests run it themselves, apart from the worker's settings.
TEST_GIT = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}
LOCAL = ("https", "file")


def git(*args: str, cwd: Path) -> str:
    done = subprocess.run(["git", *args], cwd=cwd, env=TEST_GIT, check=True, capture_output=True, text=True)
    return done.stdout.strip()


@pytest.fixture
def remote(tmp_path: Path) -> Path:
    """A bare repository, acme/app: a README on main, tagged v1."""
    bare = tmp_path / "remotes" / "acme" / "app.git"
    bare.mkdir(parents=True)
    git("init", "--quiet", "--bare", "--initial-branch=main", cwd=bare)
    seed = tmp_path / "seed"
    seed.mkdir()
    git("init", "--quiet", "--initial-branch=main", cwd=seed)
    (seed / "README.md").write_text("# app\n")
    git("add", "README.md", cwd=seed)
    git("commit", "--quiet", "-m", "First", cwd=seed)
    git("tag", "v1", cwd=seed)
    git("push", "--quiet", str(bare), "main", "v1", cwd=seed)
    return bare


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    folder = tmp_path / "run"
    folder.mkdir()
    return folder


def local(workspace: Path, **more: Any) -> Any:
    """Services for a run in ``workspace`` whose git may use a local repository."""
    return in_folder(workspace, git_protocols=LOCAL, **more)


def step(node_id: str, kind: str, **config: Any) -> dict[str, Any]:
    return node(node_id, kind, config)


# ------------------------------------------------------------------ cloning


def test_a_repository_is_cloned_into_a_folder_named_after_it(remote: Path, workspace: Path) -> None:
    clone = step("clone", "git_clone", repository=remote.as_uri())
    session = run(lambda: ran(workflow(clone), svc=local(workspace)))
    folder = workspace.resolve() / "app"
    assert session.result == {
        "folder": str(folder),
        "repository": "acme/app",
        "url": "file:///acme/app",
        "branch": "main",
        "commit": git("rev-parse", "main", cwd=remote),
        "cloned": True,
    }
    assert (folder / "README.md").read_text() == "# app\n"
    # Nothing of the step's sign-in is kept in the clone's settings.
    assert "extraheader" not in (folder / ".git" / "config").read_text().lower()


def test_a_clone_already_there_is_kept_when_a_run_is_tried_again(remote: Path, workspace: Path) -> None:
    clone = step("clone", "git_clone", repository=remote.as_uri(), folder="src")
    first = run(lambda: ran(workflow(clone), svc=local(workspace)))
    again = run(lambda: ran(workflow(clone), svc=local(workspace)))
    assert first.result["cloned"] is True
    assert again.result == {**first.result, "cloned": False}


def test_a_clone_checks_out_its_tag_and_takes_only_what_it_asks(remote: Path, workspace: Path) -> None:
    clone = step("clone", "git_clone", repository=remote.as_uri(), ref="v1", depth=1)
    session = run(lambda: ran(workflow(clone), svc=local(workspace)))
    assert session.result["branch"] is None
    assert git("rev-list", "--count", "HEAD", cwd=Path(session.result["folder"])) == "1"


@pytest.mark.parametrize(
    ("config", "says"),
    [
        ({"repository": "git@github.com:acme/app.git"}, "must be owner/name or an https:// URL"),
        ({"repository": "https://me:secret@github.com/acme/app"}, "can't hold a sign-in"),
        ({"repository": "https://10.0.0.7/acme/app"}, "private network"),
        ({"folder": "../elsewhere"}, "outside the run's folder"),
    ],
)
def test_a_clone_that_cant_be_made_takes_the_error_way(
    remote: Path, workspace: Path, config: dict[str, Any], says: str
) -> None:
    clone = step("clone", "git_clone", **{"repository": remote.as_uri(), **config})
    session = run(lambda: ran(workflow(clone, error="steps.{id}.error.message"), svc=local(workspace)))
    assert says in session.result


def test_a_folder_with_another_repository_isn_t_cloned_over(remote: Path, workspace: Path) -> None:
    (workspace / "app").mkdir()
    git("init", "--quiet", cwd=workspace / "app")
    clone = step("clone", "git_clone", repository=remote.as_uri())
    session = run(lambda: ran(workflow(clone, error="steps.{id}.error.message"), svc=local(workspace)))
    assert "already has another repository" in session.result


def test_a_local_repository_is_refused_where_only_https_is_allowed(remote: Path, workspace: Path) -> None:
    clone = step("clone", "git_clone", repository=remote.as_uri())
    session = run(lambda: ran(workflow(clone, error="steps.{id}.error.message"), svc=in_folder(workspace)))
    assert "must be owner/name or an https:// URL" in session.result


# ------------------------------------------------------- branching and pushing


def test_a_change_is_cloned_branched_committed_and_pushed(remote: Path, workspace: Path) -> None:
    nodes = [
        FILE_SYSTEM_START,
        step("clone", "git_clone", repository=remote.as_uri()),
        step("branch", "git_branch", folder="app", branch="forge/{{ input.ticket }}"),
        bash(
            "edit",
            'echo "$NOTE" > NOTES.md',
            cwd="app",
            env=[{"id": "v0", "name": "NOTE", "value": "{{ input.ticket }}"}],
        ),
        step(
            "push",
            "git_push",
            folder="{{ steps.clone.output.folder }}",
            message="Add notes for {{ input.ticket }}",
            author_name="Forge bot",
        ),
        end("done", "{ 'branch': steps.branch.output, 'push': steps.push.output }"),
    ]
    edges = [
        ("start", "next", "clone"),
        ("clone", "success", "branch"),
        ("branch", "success", "edit"),
        ("edit", "success", "push"),
        ("push", "success", "done"),
    ]
    session = run(lambda: ran(agent(nodes, edges), {"ticket": "OPS-7"}, svc=local(workspace)))
    branch, push = session.result["branch"], session.result["push"]
    assert branch["branch"] == "forge/OPS-7" and branch["created"] is True
    assert push["branch"] == "forge/OPS-7"
    assert push["committed"] is True
    assert push["files"] == ["NOTES.md"]
    assert push["repository"] == "acme/app"
    # The remote has the branch, at the commit pushed, by its author.
    assert git("rev-parse", "refs/heads/forge/OPS-7", cwd=remote) == push["commit"]
    assert git("log", "-1", "--format=%an|%s", "forge/OPS-7", cwd=remote) == "Forge bot|Add notes for OPS-7"
    folder = workspace / "app"
    assert git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}", cwd=folder) == "origin/forge/OPS-7"


def test_a_branch_already_made_is_checked_out_as_it_is(remote: Path, workspace: Path) -> None:
    run(lambda: ran(workflow(step("clone", "git_clone", repository=remote.as_uri())), svc=local(workspace)))
    folder = workspace / "app"
    git("switch", "--quiet", "--create", "feature", cwd=folder)
    (folder / "x.txt").write_text("x")
    git("add", "x.txt", cwd=folder)
    git("commit", "--quiet", "-m", "X", cwd=folder)
    git("switch", "--quiet", "main", cwd=folder)
    branch = step("branch", "git_branch", folder="app", branch="feature", start="main")
    session = run(lambda: ran(workflow(branch), svc=local(workspace)))
    assert session.result["created"] is False
    assert session.result["commit"] == git("rev-parse", "feature", cwd=folder)
    assert git("branch", "--show-current", cwd=folder) == "feature"


@pytest.mark.parametrize(
    ("config", "says"),
    [
        ({"branch": "two words"}, "isn't a name git takes for a branch"),
        ({"branch": "-f"}, "isn't a name git takes for a branch"),
        ({"branch": "ok", "start": "nowhere"}, "isn't a branch, tag or commit"),
        ({"branch": "ok", "folder": "empty"}, "isn't a git repository"),
    ],
)
def test_a_branch_that_cant_be_made_takes_the_error_way(
    remote: Path, workspace: Path, config: dict[str, Any], says: str
) -> None:
    run(lambda: ran(workflow(step("clone", "git_clone", repository=remote.as_uri())), svc=local(workspace)))
    branch = step("branch", "git_branch", **{"folder": "app", **config})
    session = run(lambda: ran(workflow(branch, error="steps.{id}.error.message"), svc=local(workspace)))
    assert says in session.result


def test_nothing_changed_pushes_the_branch_without_a_commit(remote: Path, workspace: Path) -> None:
    run(lambda: ran(workflow(step("clone", "git_clone", repository=remote.as_uri())), svc=local(workspace)))
    run(lambda: ran(workflow(step("branch", "git_branch", folder="app", branch="quiet")), svc=local(workspace)))
    push = step("push", "git_push", folder="app", message="Nothing")
    session = run(lambda: ran(workflow(push), svc=local(workspace)))
    assert session.result["committed"] is False
    assert session.result["files"] == []
    assert git("rev-parse", "refs/heads/quiet", cwd=remote) == git("rev-parse", "main", cwd=remote)


def test_a_push_off_a_branch_takes_the_error_way(remote: Path, workspace: Path) -> None:
    clone = step("clone", "git_clone", repository=remote.as_uri(), ref="v1")
    run(lambda: ran(workflow(clone), svc=local(workspace)))
    push = step("push", "git_push", folder="app", message="Detached")
    session = run(lambda: ran(workflow(push, error="steps.{id}.error.message"), svc=local(workspace)))
    assert "isn't on a branch" in session.result


def test_git_steps_need_a_start_that_allows_a_file_system() -> None:
    for kind in ("git_clone", "git_branch", "git_push"):
        document = agent([START_NODE, step("s", kind), end("done")], [("start", "next", "s"), ("s", "success", "done")])
        assert "turn on Allow file system on the start" in refused(document)


# ---------------------------------------------------------------- signing in


class Credentials:
    """The organization's one credential, cred_1: a token for github.com and api.github.com."""

    def __init__(self, params: dict[str, str] | None = None) -> None:
        self.asked: list[str] = []
        self.params = params or {}

    async def __call__(self, credential_id: str, *, organization_id: str | None, url: str) -> RequestAuth:
        self.asked.append(url)
        if credential_id != "cred_1":
            raise LookupError(f"The organization has no credential {credential_id!r}: it was deleted")
        if not url.startswith(("https://github.com/", "https://api.github.com/")):
            raise LookupError("The credential GitHub is only sent to https://github.com and https://api.github.com")
        return RequestAuth(headers={"Authorization": "Bearer t0k"}, params=self.params)


def test_a_token_signs_git_in_as_github_takes_it_never_in_its_arguments() -> None:
    settings = signed_in("https://github.com", {"Authorization": "Bearer t0k", "X-Extra": "1"})
    assert settings == [
        ("http.https://github.com/.extraHeader", "Authorization: Basic eC1hY2Nlc3MtdG9rZW46dDBr"),
        ("http.https://github.com/.extraHeader", "X-Extra: 1"),
    ]
    env = Git(environment={}, seconds=1, max_bytes=1, settings=tuple(settings)).env()
    assert env["GIT_CONFIG_VALUE_2"] == "Authorization: Basic eC1hY2Nlc3MtdG9rZW46dDBr"
    assert env["GIT_ALLOW_PROTOCOL"] == "https"
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull


@pytest.mark.parametrize(
    ("value", "url", "full_name"),
    [
        ("acme/app", "https://github.com/acme/app.git", "acme/app"),
        ("github.com/acme/app.git", "https://github.com/acme/app.git", "acme/app"),
        ("https://github.com/acme/app/", "https://github.com/acme/app", "acme/app"),
        ("https://git.acme.dev/team/app.git", "https://git.acme.dev/team/app.git", "team/app"),
    ],
)
def test_a_repository_is_owner_and_name_or_its_https_url(value: str, url: str, full_name: str) -> None:
    found = repository(value)
    assert (found.url, found.full_name) == (url, full_name)


def test_github_enterprise_has_its_api_under_its_own_host() -> None:
    assert repository("acme/app").api_url == "https://api.github.com"
    assert repository("https://git.acme.dev/team/app").api_url == "https://git.acme.dev/api/v3"


@pytest.mark.parametrize("value", ["ssh://git@github.com/acme/app", "ext::sh -c id", "file:///etc", "acme"])
def test_only_https_repositories_are_reached(value: str) -> None:
    with pytest.raises(StepFailed):
        repository(value)


def test_a_credential_that_cant_sign_git_in_takes_the_error_way_before_git_runs(workspace: Path) -> None:
    for credential, says in [("cred_9", "it was deleted"), ("cred_1", "only sent to")]:
        repo = "https://elsewhere.example.com/acme/app" if credential == "cred_1" else "acme/app"
        clone = step("clone", "git_clone", repository=repo, credential=credential)
        svc = in_folder(
            workspace,
            credentials=Credentials(),
            organization_id="org_1",
            allowed_hosts=("github.com", "elsewhere.example.com"),
        )
        session = run(lambda c=clone, s=svc: ran(workflow(c, error="steps.{id}.error.message"), svc=s))
        assert says in session.result
    # Refused before git ran: nothing was cloned.
    assert list(workspace.iterdir()) == []


def test_a_credential_that_sends_its_key_in_the_query_can_t_sign_git_in(workspace: Path) -> None:
    clone = step("clone", "git_clone", repository="acme/app", credential="cred_1")
    svc = in_folder(
        workspace, credentials=Credentials({"key": "k1"}), organization_id="org_1", allowed_hosts=("github.com",)
    )
    session = run(lambda: ran(workflow(clone, error="steps.{id}.error.message"), svc=svc))
    assert "sends its key in the URL's query" in session.result


# -------------------------------------------------------------- pull requests


def pull_request(**config: Any) -> dict[str, Any]:
    settings = {"repository": "acme/app", "head": "forge/OPS-7", "title": "Notes for OPS-7", "credential": "cred_1"}
    return step("pr", "github_pull_request", **{**settings, **config})


def opened(number: int = 42, head: str = "forge/OPS-7", base: str = "main") -> dict[str, Any]:
    return {
        "number": number,
        "html_url": f"https://github.com/acme/app/pull/{number}",
        "title": "Notes for OPS-7",
        "state": "open",
        "draft": False,
        "head": {"ref": head},
        "base": {"ref": base},
    }


def github_services(api: Any) -> Any:
    return services(
        api, credentials=Credentials(), organization_id="org_1", allowed_hosts=("api.github.com", "github.com")
    )


def pr_workflow(pr: dict[str, Any]) -> dict[str, Any]:
    return agent(
        [START_NODE, pr, end("done", "steps.pr.output"), end("failed", "steps.pr.error.message", outcome="failed")],
        [("start", "next", "pr"), ("pr", "success", "done"), ("pr", "error", "failed")],
    )


def test_a_pull_request_is_opened_into_the_default_branch() -> None:
    seen: list[httpx.Request] = []

    def api(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "GET" and request.url.path == "/repos/acme/app":
            return httpx.Response(200, json={"default_branch": "main"})
        if request.method == "POST" and request.url.path == "/repos/acme/app/pulls":
            return httpx.Response(201, json=opened())
        return httpx.Response(404, json={"message": "Not Found"})

    pr = pull_request(body="Made by {{ input.who }}", draft=True)
    session = run(lambda: ran(pr_workflow(pr), {"who": "Forge"}, svc=github_services(api)))
    assert session.result == {
        "number": 42,
        "url": "https://github.com/acme/app/pull/42",
        "title": "Notes for OPS-7",
        "state": "open",
        "draft": False,
        "head": "forge/OPS-7",
        "base": "main",
        "created": True,
    }
    sent = json.loads(seen[1].content)
    assert sent == {
        "title": "Notes for OPS-7",
        "head": "forge/OPS-7",
        "base": "main",
        "body": "Made by Forge",
        "draft": True,
    }
    assert all(request.headers["authorization"] == "Bearer t0k" for request in seen)
    assert seen[1].headers["accept"] == "application/vnd.github+json"


def test_a_pull_request_already_open_for_its_branch_is_the_one_handed_on() -> None:
    def api(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                422,
                json={
                    "message": "Validation Failed",
                    "errors": [{"message": "A pull request already exists for acme:forge/OPS-7."}],
                },
            )
        if request.url.path == "/repos/acme/app/pulls":
            assert dict(request.url.params) == {"head": "acme:forge/OPS-7", "base": "develop", "state": "open"}
            return httpx.Response(200, json=[opened(7, base="develop")])
        return httpx.Response(404)

    session = run(lambda: ran(pr_workflow(pull_request(base="develop")), svc=github_services(api)))
    assert session.result["number"] == 7
    assert session.result["created"] is False


def test_a_pull_request_github_refuses_takes_the_error_way_with_why() -> None:
    def api(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422,
            json={"message": "Validation Failed", "errors": [{"message": "No commits between main and forge/OPS-7"}]},
        )

    session = run(lambda: ran(pr_workflow(pull_request(base="main")), svc=github_services(api)))
    assert session.result == "GitHub answered 422: Validation Failed; No commits between main and forge/OPS-7"


@pytest.mark.parametrize(
    ("config", "says"),
    [({"head": " "}, "Its branch, the one it merges, is empty"), ({"title": ""}, "Its title is empty")],
)
def test_a_pull_request_needs_its_branch_and_title(config: dict[str, Any], says: str) -> None:
    session = run(lambda: ran(pr_workflow(pull_request(**config)), svc=github_services(lambda r: httpx.Response(500))))
    assert session.result == says
