"""
Files a run starts with: how a start's rule takes them (their names, types
and media types), and a run reading them as ADK artifacts of its session:
its start lists them in ``state.files``, its LLM agents load them with ADK's
``load_artifacts`` tool, and starting over in a new session takes them along.
"""

from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import pytest
from google.adk.artifacts import FileArtifactService, InMemoryArtifactService
from google.adk.runners import Runner
from google.adk.sessions import DatabaseSessionService
from google.genai import types

from forge_task_adk_workflows.config import AdkWorkflowsSettings
from forge_task_adk_workflows.files import (
    FILE_TYPES,
    FileRefused,
    FilesRule,
    RunFile,
    artifact_service,
    checked_file,
    clean_name,
    files_rule,
    save_run_files,
    unique_names,
)
from forge_task_adk_workflows.runs import APP_NAME
from forge_task_adk_workflows.task import build_artifacts
from forge_tasks.errors import TransientError
from forge_tasks.tasks import JobStatus

from .scripted_llm import ScriptedLlm
from .test_graph import end, llm_config, node, transform
from .test_task import MEMBER, SESSION, line, overloaded, payload, worker

FILES_START = node("start", "start", {"input_schema": {}, "allow_files": True, "file_types": ["text", "pdf"]})


def files_line(*steps: dict[str, Any]) -> dict[str, Any]:
    """``test_task.line`` from a start that takes files."""
    document = line(*steps)
    document["nodes"][0] = FILES_START
    return document


@pytest.fixture
async def sessions(tmp_path: Path) -> AsyncGenerator[DatabaseSessionService]:
    service = DatabaseSessionService(db_url=f"sqlite+aiosqlite:///{tmp_path / 'sessions.db'}")
    yield service
    await service.close()


async def saved(artifacts: InMemoryArtifactService, *files: tuple[str, bytes]) -> list[RunFile]:
    """The files saved in the run's session, as the admin API saves them."""
    return await save_run_files(
        artifacts,
        app_name=APP_NAME,
        user_id=MEMBER,
        session_id=SESSION,
        files=[(name, "text", "text/plain", data) for name, data in files],
    )


# ------------------------------------------------------------------ the rules


def test_a_start_s_rule_is_its_allow_files_and_file_types() -> None:
    assert files_rule({"nodes": [FILES_START]}) == FilesRule(allowed=True, types=("text", "pdf"))
    assert files_rule({"nodes": [node("start", "start", {"input_schema": {}})]}) == FilesRule()
    assert files_rule({"nodes": []}) == FilesRule()
    # Only a true allows them.
    assert not files_rule({"nodes": [node("start", "start", {"allow_files": "yes"})]}).allowed


@pytest.mark.parametrize(
    ("sent", "kept"),
    [
        ("report.pdf", "report.pdf"),
        ("../../etc/passwd", "passwd"),
        ("C:\\Users\\ada\\notes.txt", "notes.txt"),
        ("user:secrets.txt", "secrets.txt"),
        ("USER: user:x.pdf", "x.pdf"),
        ("bad\x00name\n.txt", "badname.txt"),
        ("", "file"),
        (None, "file"),
        ("...", "file"),
        ("a" * 300 + ".pdf", "a" * 196 + ".pdf"),
    ],
)
def test_a_file_keeps_the_last_part_of_its_name_never_the_user_s_scope(sent: str | None, kept: str) -> None:
    assert clean_name(sent) == kept


def test_files_with_the_same_name_are_numbered() -> None:
    assert unique_names(["a.pdf", "A.pdf", "a.pdf", "b", "b"]) == ["a.pdf", "A (2).pdf", "a (3).pdf", "b", "b (2)"]


def test_a_file_of_a_type_the_start_takes_has_its_type_s_media_type() -> None:
    rule = FilesRule(allowed=True, types=("pdf", "word"))
    assert checked_file(rule, "Q3.PDF", "application/octet-stream") == ("Q3.PDF", "pdf", "application/pdf")
    assert checked_file(rule, "old.doc", None) == ("old.doc", "word", "application/msword")


@pytest.mark.parametrize("name", ["run.exe", "notes.txt", "no-extension", "pdf"])
def test_a_file_of_a_type_the_start_doesnt_take_is_refused(name: str) -> None:
    with pytest.raises(FileRefused, match=r"takes pdf, word files only"):
        checked_file(FilesRule(allowed=True, types=("pdf", "word")), name, "application/pdf")


def test_a_start_that_takes_no_files_refuses_any() -> None:
    with pytest.raises(FileRefused, match="doesn't take files"):
        checked_file(FilesRule(), "a.pdf", "application/pdf")


def test_a_start_that_takes_any_type_keeps_what_it_was_sent_as() -> None:
    anything = FilesRule(allowed=True)
    assert checked_file(anything, "a.png", "text/plain") == ("a.png", "png", "image/png")
    assert checked_file(anything, "data.parquet", "application/vnd.apache.parquet") == (
        "data.parquet",
        "",
        "application/vnd.apache.parquet",
    )
    # A media type that isn't one, or says nothing, is guessed from the name.
    assert checked_file(anything, "page.svg", "<script>")[2] == "image/svg+xml"
    assert checked_file(anything, "blob.bin", "application/octet-stream")[2] == "application/octet-stream"
    assert checked_file(anything, "mystery", None)[2] == "application/octet-stream"


def test_every_type_names_its_extensions_in_lower_case() -> None:
    for file_type in FILE_TYPES.values():
        assert file_type.extensions
        for extension, media_type in file_type.extensions.items():
            assert extension == extension.lower() and extension.startswith(".")
            assert "/" in media_type


# ----------------------------------------------------------- where they're kept


def test_artifacts_are_kept_in_memory_a_folder_or_s3(tmp_path: Path) -> None:
    assert isinstance(artifact_service("memory"), InMemoryArtifactService)
    assert isinstance(artifact_service(str(tmp_path)), FileArtifactService)
    assert isinstance(artifact_service(f"file://{tmp_path}"), FileArtifactService)
    s3 = artifact_service(
        "s3://forge-artifacts/workflows",
        endpoint_url="http://127.0.0.1:19010",
        region="us-east-1",
        access_key_id="forge-local",
        secret_access_key="secret",
    )
    assert (type(s3).__name__, s3.bucket, s3.prefix) == ("S3ArtifactService", "forge-artifacts", "workflows/")  # type: ignore[attr-defined]
    assert s3.client.meta.endpoint_url == "http://127.0.0.1:19010"  # type: ignore[attr-defined]
    with pytest.raises(ValueError, match="not gs://"):
        artifact_service("gs://bucket")


def test_a_worker_keeps_artifacts_where_its_settings_say() -> None:
    artifacts, why = build_artifacts(AdkWorkflowsSettings(artifacts="memory"))
    assert isinstance(artifacts, InMemoryArtifactService) and why is None
    assert build_artifacts(AdkWorkflowsSettings()) == (None, None)
    artifacts, why = build_artifacts(AdkWorkflowsSettings(artifacts="ftp://x"))
    assert artifacts is None and why is not None and "ftp://" in why


# ------------------------------------------------------------------ runs


async def test_a_run_lists_its_files_in_the_state_and_its_nodes_read_them(
    sessions: DatabaseSessionService,
) -> None:
    artifacts = InMemoryArtifactService()
    files = await saved(artifacts, ("notes.txt", b"The answer is 42."))
    llm = ScriptedLlm(
        turns=[
            [types.Part(function_call=types.FunctionCall(name="load_artifacts", args={"artifact_names": ["notes.txt"]}))],
            [types.Part(text="42")],
        ]
    )
    w = worker(sessions, model=llm, artifacts=artifacts)
    document = files_line(
        transform("names", "state.files.name"),
        node("read", "llm", llm_config(instruction="What's the answer?")),
        end("done", '{"names": steps.names.output, "said": steps.read.output, "files": state.files}'),
    )

    result = await w.run(payload(document, {}, files=files))

    assert result.status is JobStatus.OK, result.error
    assert result.detail["result"] == {
        "names": "notes.txt",
        "said": "42",
        "files": [{"name": "notes.txt", "media_type": "text/plain", "size_bytes": 17, "type": "text", "version": 0}],
    }
    first, second = llm.requests
    # The agent is told the files' names, and given the tool that loads them…
    assert "load_artifacts" in first.tools_dict
    assert '["notes.txt"]' in "\n".join(str(first.config.system_instruction).split("\\n"))
    # …and reads the one it asked for.
    sent = [part.text for content in second.contents for part in content.parts or [] if part.text]
    assert "Artifact notes.txt is:" in sent
    assert "The answer is 42." in sent


async def test_a_start_that_allows_files_lists_none_when_the_run_has_none(sessions: DatabaseSessionService) -> None:
    llm = ScriptedLlm(turns=[[types.Part(text="Hi")]])
    w = worker(sessions, model=llm)
    document = files_line(node("say", "llm", llm_config()), end("done", "state.files"))

    result = await w.run(payload(document, {}))

    assert result.detail["result"] == []
    # Without files, no agent loads any.
    assert "load_artifacts" not in llm.requests[0].tools_dict


async def test_a_run_with_files_fails_on_a_worker_that_keeps_no_artifacts(sessions: DatabaseSessionService) -> None:
    files = await saved(InMemoryArtifactService(), ("notes.txt", b"x"))
    w = worker(sessions)

    result = await w.run(payload(files_line(end("done")), {}, files=files))

    assert result.status is JobStatus.FAILED
    assert result.error is not None
    assert result.error.startswith("The run started with files, and this worker can't read them")
    assert "HYBRID_ADK_WORKFLOWS__ARTIFACTS" in result.error


async def test_a_run_that_starts_over_takes_its_files_to_its_new_session(
    sessions: DatabaseSessionService, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifacts = InMemoryArtifactService()
    files = await saved(artifacts, ("notes.txt", b"kept"))
    llm = ScriptedLlm(turns=[overloaded(), [types.Part(text="Hi!")]])
    w = worker(sessions, model=llm, artifacts=artifacts)
    run = payload(
        files_line(
            node("triage", "llm", llm_config(instruction="Say hi")),
            end("done", "state.files.name"),
        ),
        {},
        files=files,
    )
    with pytest.raises(TransientError):
        await w.run(run)

    carry_on = Runner.run_async

    def refusing(self: Runner, **options: Any) -> Any:
        if options.get("invocation_id") and options.get("new_message") is None:

            async def refuse() -> AsyncGenerator[Any]:
                raise ValueError("that invocation can't be resumed")
                yield

            return refuse()
        return carry_on(self, **options)

    monkeypatch.setattr(Runner, "run_async", refusing)
    result = await w.run(run)

    assert result.status is JobStatus.OK, result.error
    assert result.detail["session_id"] == f"{SESSION}-r1"
    assert result.detail["result"] == "notes.txt"
    copied = await artifacts.load_artifact(
        app_name=APP_NAME, user_id=MEMBER, session_id=f"{SESSION}-r1", filename="notes.txt"
    )
    assert copied is not None and copied.inline_data is not None
    assert copied.inline_data.data == b"kept"


async def test_a_run_whose_files_are_gone_cant_start_over(
    sessions: DatabaseSessionService, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = [RunFile(name="gone.txt", media_type="text/plain", size_bytes=1, type="text")]
    w = worker(sessions, model=ScriptedLlm(turns=[overloaded()]), artifacts=InMemoryArtifactService())
    run = payload(files_line(node("triage", "llm", llm_config()), end("done")), {}, files=files)
    with pytest.raises(TransientError):
        await w.run(run)

    def refusing(self: Runner, **options: Any) -> Any:
        async def refuse() -> AsyncGenerator[Any]:
            raise ValueError("that invocation can't be resumed")
            yield

        return refuse()

    monkeypatch.setattr(Runner, "run_async", refusing)
    result = await w.run(run)

    assert result.status is JobStatus.FAILED
    assert result.error == "The run can't start over: The run's file gone.txt isn't kept any more"
