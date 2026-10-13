"""
The example workflows (examples/workflows/*.agent.json) build, and the
repository change scout runs: each repository cloned (from local remotes here,
through git's insteadOf) and explored by a Pi agent (the stand-in pi), several
at once, a repository that can't be cloned noted, and the findings merged.
"""

import json
import shlex
import subprocess
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import pytest
from forge_common.adk.models import ProviderModels
from forge_common.model_provider import parse_model_provider_yaml
from forge_pi_agent import fake_pi
from google.adk.sessions import DatabaseSessionService

from forge_task_adk_workflows.graph import build_agent
from forge_tasks.tasks import JobStatus

from .test_pi_steps import MODELS
from .test_task import payload, worker

EXAMPLES = Path(__file__).resolve().parents[4] / "examples" / "workflows"
SCOUT = EXAMPLES / "repo-change-scout.agent.json"
FINDINGS = {
    "needs_change": True,
    "summary": "The login has no hook for SSO.",
    "changes": [{"file": "app.py", "symbol": "login", "what": "Call the SSO provider."}],
}


@pytest.fixture
async def sessions(tmp_path: Path) -> AsyncGenerator[DatabaseSessionService]:
    service = DatabaseSessionService(db_url=f"sqlite+aiosqlite:///{tmp_path / 'sessions.db'}")
    yield service
    await service.close()


def remote(root: Path, name: str) -> Path:
    """A bare repository with one commit, as GitHub would serve ``name``."""
    work = root / "work" / name
    work.mkdir(parents=True)
    (work / "app.py").write_text("def login(user):\n    return user\n")
    for command in (
        ["git", "init", "--quiet", "--initial-branch=main"],
        ["git", "add", "."],
        ["git", "-c", "user.name=Ada", "-c", "user.email=ada@example.com", "commit", "--quiet", "-m", "First"],
    ):
        subprocess.run(command, cwd=work, check=True)
    bare = root / "remotes" / f"{name}.git"
    subprocess.run(["git", "clone", "--quiet", "--bare", str(work), str(bare)], check=True)
    return bare


@pytest.mark.parametrize("path", sorted(EXAMPLES.glob("*.agent.json")), ids=lambda path: path.name)
def test_every_example_builds(path: Path) -> None:
    build_agent(json.loads(path.read_text()))


async def test_the_scout_explores_each_repository_and_merges_what_they_found(
    sessions: DatabaseSessionService, tmp_path: Path
) -> None:
    remote(tmp_path, "acme/billing")
    remote(tmp_path, "acme/portal")
    w = worker(
        sessions,
        model=ProviderModels(parse_model_provider_yaml(MODELS, {})),
        workspaces=str(tmp_path / "volume"),
        commands="local",
        pi_agent="local",
        pi_command=shlex.join(fake_pi.COMMAND),
    )
    # GitHub is the local remotes: what the worker's environment lets commands read.
    w.task.services = w.task.services.but(
        command_environment={
            **w.task.services.command_environment,
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": f"url.file://{tmp_path / 'remotes'}/.insteadOf",
            "GIT_CONFIG_VALUE_0": "https://github.com/",
        }
    )
    document = json.loads(SCOUT.read_text())
    request = {"repos": ["acme/billing", "acme/portal", "acme/missing"], "prompt": "Support SSO logins."}

    with pytest.MonkeyPatch.context() as patch:
        # The stand-in pi's first answer isn't JSON; asked again, it's the findings.
        patch.setattr("forge_task_adk_workflows.graph.factories.pi.PI_ENV", {"FAKE_PI_FIX": json.dumps(FINDINGS)})
        result = await w.run(payload(document, request))

    assert result.status is JobStatus.OK, result.error
    merged: dict[str, Any] = result.detail["result"]
    assert merged["request"] == "Support SSO logins."
    assert merged["repositories"] == 3
    assert merged["need_changes"] == ["acme/billing", "acme/portal"]
    assert merged["no_changes"] == []
    [missing] = merged["not_cloned"]
    assert missing["repo"] == "acme/missing"
    assert missing["cloned"] is False
    assert "acme/missing" in missing["error"]
    assert [finding["repo"] for finding in merged["findings"]] == ["acme/billing", "acme/portal"]
    for finding in merged["findings"]:
        assert finding["needs_change"] is True
        assert finding["changes"] == FINDINGS["changes"]
        assert finding["commit"].endswith(" First")
    # Each repository in a folder of its own in the run's folder, with no token in its remote.
    run = next((tmp_path / "volume").glob("*/*/local"))
    assert sorted(path.name for path in (run / "repos").iterdir()) == [
        "0_acme_billing",
        "1_acme_portal",
        "2_acme_missing",
    ]
    config = (run / "repos" / "0_acme_billing" / ".git" / "config").read_text()
    assert "url = https://github.com/acme/billing.git" in config
    assert "extraHeader" not in config


async def test_the_scout_clones_with_the_worker_s_github_token_in_a_header_only(
    sessions: DatabaseSessionService, tmp_path: Path
) -> None:
    seen = tmp_path / "seen.txt"
    git = tmp_path / "bin" / "git"
    git.parent.mkdir()
    # A git that notes what it's run with, and clones nothing.
    git.write_text(f'#!/bin/bash\nprintf "%s\\n" "$@" >> {shlex.quote(str(seen))}\nexit 1\n')
    git.chmod(0o755)
    w = worker(sessions, workspaces=str(tmp_path / "volume"), commands="local", pi_agent="local")
    w.task.services = w.task.services.but(
        command_environment={
            **w.task.services.command_environment,
            "PATH": f"{git.parent}:{w.task.services.command_environment.get('PATH', '')}",
            "GITHUB_TOKEN": "ghp_secret",
        }
    )

    result = await w.run(payload(json.loads(SCOUT.read_text()), {"repos": ["acme/billing"], "prompt": "x"}))

    assert result.status is JobStatus.OK, result.error
    args = seen.read_text().splitlines()
    assert "-c" in args
    header = args[args.index("-c") + 1]
    # x-access-token:ghp_secret, as Basic auth: never the token itself, nor in the URL.
    assert header == "http.extraHeader=Authorization: Basic eC1hY2Nlc3MtdG9rZW46Z2hwX3NlY3JldA=="
    assert "https://github.com/acme/billing.git" in args
    assert "ghp_secret" not in seen.read_text()
    assert result.detail["result"]["not_cloned"][0]["repo"] == "acme/billing"
