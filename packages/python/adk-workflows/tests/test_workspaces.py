"""
A run's file system: a folder of its own on the worker's volume, for a
workflow whose start allows one, kept in its state as ``workspace`` for its
steps and its LLM agents' instructions.
"""

from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import pytest
from google.adk.sessions import DatabaseSessionService
from google.genai import types

from forge_task_adk_workflows.workspaces import (
    WorkspaceError,
    allows_file_system,
    folder_name,
    make_workspace,
    uses_file_system,
    workspace_of,
)
from forge_tasks.tasks import JobStatus

from .scripted_llm import ScriptedLlm
from .test_graph import START_NODE, agent, end, llm_config, node, transform
from .test_task import ORGANIZATION, Control, line, payload, worker

FILE_SYSTEM_START = node("start", "start", {"input_schema": {}, "allow_file_system": True})


def file_system_line(*steps: dict[str, Any]) -> dict[str, Any]:
    """``test_task.line`` from a start that allows a file system."""
    document = line(*steps)
    document["nodes"][0] = FILE_SYSTEM_START
    return document


@pytest.fixture
async def sessions(tmp_path: Path) -> AsyncGenerator[DatabaseSessionService]:
    service = DatabaseSessionService(db_url=f"sqlite+aiosqlite:///{tmp_path / 'sessions.db'}")
    yield service
    await service.close()


# ------------------------------------------------------------------ the folder


def test_only_a_start_that_says_true_allows_a_file_system() -> None:
    assert allows_file_system({"nodes": [FILE_SYSTEM_START]})
    assert not allows_file_system({"nodes": [START_NODE]})
    assert not allows_file_system({"nodes": [node("start", "start", {"allow_file_system": "yes"})]})
    assert not allows_file_system({"nodes": []})


def test_a_saved_workflow_s_start_counts_however_deep() -> None:
    inner = agent([FILE_SYSTEM_START], [], agent_id="ag_inner00002")
    middle = agent([START_NODE, node("deeper", "saved", {"agent": "ag_inner00002"})], [], agent_id="ag_middle0003")
    outer = line(node("nested", "saved", {"agent": "ag_middle0003", "version": 2}))
    saved = {"ag_middle0003@2": middle, "ag_inner00002": inner}

    assert uses_file_system(outer, saved)
    assert not uses_file_system(outer, {"ag_middle0003@2": middle})
    # A workflow that runs itself is looked at once.
    looping = agent([START_NODE, node("again", "saved", {"agent": "ag_test000001"})], [])
    assert not uses_file_system(looping, {"ag_test000001": looping})


@pytest.mark.parametrize("plain", ["ag_x1", "3f6c0000-0000-4000-8000-000000000001", "run.1", "a" * 128])
def test_a_plain_id_is_its_own_folder(plain: str) -> None:
    assert folder_name(plain) == plain


@pytest.mark.parametrize("odd", ["..", ".", "", "../etc", "a/b", "a\\b", ".hidden", "a b", "a" * 129, "é"])
def test_any_other_id_is_a_hash_that_stays_in_its_folder(odd: str) -> None:
    name = folder_name(odd)
    assert name.startswith("id-")
    assert len(name) == 35
    assert name == folder_name(odd)


def test_a_run_s_folder_is_under_its_organization_and_workflow(tmp_path: Path) -> None:
    folder = make_workspace(tmp_path / "volume", organization="org_1", workflow="ag_x", run="../../escape")

    assert folder.parent == tmp_path / "volume" / "org_1" / "ag_x"
    assert folder.is_dir()
    assert folder.stat().st_mode & 0o777 == 0o700
    # Made again, it's the same folder, with what's in it.
    (folder / "repo").mkdir()
    assert make_workspace(tmp_path / "volume", organization="org_1", workflow="ag_x", run="../../escape") == folder
    assert (folder / "repo").is_dir()


def test_a_folder_that_cant_be_made_says_why(tmp_path: Path) -> None:
    (tmp_path / "volume").write_text("not a folder")

    with pytest.raises(WorkspaceError, match=r"can't be made"):
        make_workspace(tmp_path / "volume", organization="org_1", workflow="ag_x", run="r1")


def test_a_folder_reached_through_a_link_elsewhere_is_refused(tmp_path: Path) -> None:
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "volume").mkdir()
    (tmp_path / "volume" / "org_1").symlink_to(tmp_path / "elsewhere")

    with pytest.raises(WorkspaceError, match=r"leads outside"):
        make_workspace(tmp_path / "volume", organization="org_1", workflow="ag_x", run="r1")


# ------------------------------------------------------------------ runs


async def test_a_run_works_in_its_own_folder_its_steps_and_agents_read(
    sessions: DatabaseSessionService, tmp_path: Path
) -> None:
    volume = tmp_path / "volume"
    llm = ScriptedLlm(turns=[[types.Part(text="Cloned")]])
    w = worker(sessions, model=llm, workspaces=str(volume))
    document = file_system_line(
        transform("where", "state.workspace & '/backend'"),
        node("clone", "llm", llm_config(instruction="Clone the backend into {{ steps.where.output }}.")),
        end("done", "state.workspace"),
    )

    result = await w.run(payload(document, {}))

    assert result.status is JobStatus.OK, result.error
    folder = workspace_of(volume, organization=ORGANIZATION, workflow=document["id"], run="local")
    assert result.detail["result"] == str(folder)
    assert folder.is_dir()
    assert f"Clone the backend into {folder}/backend." in str(llm.requests[0].config.system_instruction)


async def test_each_run_has_its_own_folder_and_each_attempt_the_same_one(
    sessions: DatabaseSessionService, tmp_path: Path
) -> None:
    w = worker(sessions, workspaces=str(tmp_path / "volume"))
    document = file_system_line(end("done", "state.workspace"))

    first = await w.run(payload(document, {}))
    again = await w.run(payload(document, {}))
    w.control = Control(instance="another-run")
    other = await w.run(payload(document, {}).model_copy(update={"session_id": "5b1e0c4e-0000-4000-8000-0000000000aa"}))

    assert first.detail["result"] == again.detail["result"]
    assert other.detail["result"] != first.detail["result"]
    assert Path(other.detail["result"]).name == "another-run"


async def test_a_start_that_doesnt_allow_one_has_no_folder(sessions: DatabaseSessionService, tmp_path: Path) -> None:
    w = worker(sessions, workspaces=str(tmp_path / "volume"))

    result = await w.run(payload(line(end("done", "$exists(state.workspace)")), {}))

    assert result.detail["result"] is False
    assert not (tmp_path / "volume").exists()


async def test_a_saved_workflow_that_allows_one_works_in_the_run_s(
    sessions: DatabaseSessionService, tmp_path: Path
) -> None:
    w = worker(sessions, workspaces=str(tmp_path / "volume"))
    inner = agent(
        [FILE_SYSTEM_START, end("done", "state.workspace")], [("start", "next", "done")], agent_id="ag_inner00002"
    )
    outer = line(node("nested", "saved", {"agent": "ag_inner00002"}), end("done", "steps.nested.output"))

    result = await w.run(payload(outer, {}, saved={"ag_inner00002": inner}))

    assert result.status is JobStatus.OK, result.error
    assert result.detail["result"] == str(
        workspace_of(tmp_path / "volume", organization=ORGANIZATION, workflow=outer["id"], run="local")
    )


async def test_a_run_that_needs_one_fails_on_a_worker_without_a_volume(sessions: DatabaseSessionService) -> None:
    w = worker(sessions)

    result = await w.run(payload(file_system_line(end("done")), {}))

    assert result.status is JobStatus.FAILED
    assert result.error is not None
    assert result.error.startswith("The workflow works in a file system, and this worker has none")
    assert "HYBRID_ADK_WORKFLOWS__WORKSPACES" in result.error


async def test_a_run_whose_folder_cant_be_made_fails_saying_why(
    sessions: DatabaseSessionService, tmp_path: Path
) -> None:
    (tmp_path / "volume").write_text("not a folder")
    w = worker(sessions, workspaces=str(tmp_path / "volume"))

    result = await w.run(payload(file_system_line(end("done")), {}))

    assert result.status is JobStatus.FAILED
    assert result.error is not None
    assert result.error.startswith("The run's file system can't be used: its folder")
