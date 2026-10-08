"""Files a workflow run starts with, as outside apps send them: a multipart
form or base64 in JSON over REST, file parts over A2A. Each is held to the
start (allowed, of its types, within the limits) and saved as an ADK
artifact of the run's session, where the worker's runs read it."""

import base64
import json
import uuid
from typing import Any

import httpx
import pytest
from forge_task_adk_workflows.files import FILE_TYPES
from key_world import World
from runtime_world import PAPERS, SHIP, Runtime
from test_workflow_a2a import rpc

from forge_admin.adk_workflows.documents import SCHEMA_PATH

PDF = b"%PDF-1.7 a small report"
INPUT = {"key": "a1"}


@pytest.fixture
def runtime(make_world: Any) -> Runtime:
    return Runtime(make_world(workflow_runtime_wait=0.2))


def upload(
    runtime: Runtime,
    files: list[tuple[str, bytes, str]],
    ref: str = PAPERS,
    **fields: str,
) -> httpx.Response:
    """Start a run as a multipart form: the input as JSON text, each file a part."""
    client = runtime.world.client
    return client.post(
        f"/api/v1/runtime/workflows/{ref}/runs",
        data={"input": json.dumps(INPUT), **fields},
        files=[("files", file) for file in files],
    )


def kept(runtime: Runtime, run: dict[str, Any], name: str) -> bytes | None:
    """A run's file as its session's artifact keeps it."""
    stored = runtime.call(runtime.runs.get, run["id"])
    payload = stored["payload"]

    async def load() -> Any:
        return await runtime.artifacts.load_artifact(
            app_name="adk_workflows",
            user_id=payload["run_as"],
            session_id=payload["session_id"],
            filename=name,
        )

    part = runtime.call(load)
    return part.inline_data.data if part is not None else None


# ------------------------------------------------------------------ REST


def test_a_workflow_says_what_files_it_takes(runtime: Runtime) -> None:
    info = runtime.send("GET", f"/{PAPERS}").json()
    assert info["files"] == {
        "allowed": True,
        "types": {"pdf": ["pdf"], "text": ["txt"]},
        "max_count": 10,
        "max_bytes": 20 * 1024 * 1024,
    }
    assert runtime.send("GET", f"/{SHIP}").json()["files"]["allowed"] is False


def test_a_run_started_with_files_saves_them_in_its_session(runtime: Runtime) -> None:
    started = upload(
        runtime,
        [
            ("report.pdf", PDF, "application/octet-stream"),
            ("notes.txt", b"hello", "text/plain"),
        ],
        wait="0",
    )
    assert started.status_code == 201, started.json()
    run = started.json()
    assert run["files"] == [
        {
            "name": "report.pdf",
            "media_type": "application/pdf",
            "size_bytes": len(PDF),
            "type": "pdf",
            "version": 0,
        },
        {
            "name": "notes.txt",
            "media_type": "text/plain",
            "size_bytes": 5,
            "type": "text",
            "version": 0,
        },
    ]
    stored = runtime.call(runtime.runs.get, run["id"])
    assert stored["payload"]["input"] == INPUT
    assert stored["payload"]["files"] == run["files"]
    assert runtime.queue.queued == [run["id"]]
    assert kept(runtime, run, "report.pdf") == PDF
    assert kept(runtime, run, "notes.txt") == b"hello"
    # Following the run lists them too.
    assert (
        runtime.send("GET", f"/{PAPERS}/runs/{run['id']}").json()["files"]
        == run["files"]
    )


def test_files_come_as_base64_in_json_too(runtime: Runtime) -> None:
    started = runtime.send(
        "POST",
        f"/{PAPERS}/runs",
        {
            "input": INPUT,
            "files": [
                {"name": "a.txt", "content": base64.b64encode(b"one").decode()},
                {
                    "name": "dir/A.txt",
                    "content": base64.b64encode(b"two").decode(),
                    "media_type": "text/csv",
                },
            ],
        },
    )
    assert started.status_code == 201, started.json()
    run = started.json()
    # A path is dropped from a name, and a name taken twice is numbered.
    assert [(f["name"], f["media_type"]) for f in run["files"]] == [
        ("a.txt", "text/plain"),
        ("A (2).txt", "text/plain"),
    ]
    assert kept(runtime, run, "A (2).txt") == b"two"


@pytest.mark.parametrize(
    ("ref", "files", "status", "said"),
    [
        (
            PAPERS,
            [("notes.md", b"# hi", "text/markdown")],
            422,
            "notes.md: the workflow's start takes pdf, text files only",
        ),
        (
            SHIP,
            [("report.pdf", PDF, "application/pdf")],
            422,
            "The workflow's start doesn't take files",
        ),
        (PAPERS, [("empty.pdf", b"", "application/pdf")], 422, "empty.pdf is empty"),
        (
            PAPERS,
            [(f"{n}.txt", b"x", "text/plain") for n in range(11)],
            400,
            "Too many files",
        ),
    ],
)
def test_files_the_start_doesnt_take_are_refused_and_nothing_runs(
    runtime: Runtime,
    ref: str,
    files: list[tuple[str, bytes, str]],
    status: int,
    said: str,
) -> None:
    refused = upload(runtime, files, ref=ref)
    assert refused.status_code == status, refused.json()
    assert said in refused.json()["detail"]
    assert runtime.queue.queued == []


def test_a_file_too_big_is_refused(make_world: Any) -> None:
    runtime = Runtime(make_world(workflow_files_max_bytes=8))
    refused = upload(runtime, [("report.pdf", PDF, "application/pdf")])
    assert refused.status_code == 413
    assert (
        refused.json()["detail"]
        == "report.pdf is bigger than a run's files may be (8 bytes)"
    )
    as_json = runtime.send(
        "POST",
        f"/{PAPERS}/runs",
        {
            "input": INPUT,
            "files": [{"name": "r.pdf", "content": base64.b64encode(PDF).decode()}],
        },
    )
    assert as_json.status_code == 413
    assert runtime.queue.queued == []


def test_what_isnt_a_run_s_body_is_refused(runtime: Runtime) -> None:
    not_json = upload(runtime, [], input="{nope")
    assert not_json.status_code == 422
    assert not_json.json()["detail"][0]["loc"] == ["body", "input"]
    unknown = upload(runtime, [], colour="blue")
    assert unknown.status_code == 422
    assert unknown.json()["detail"][0]["loc"] == ["body", "colour"]
    not_base64 = runtime.send(
        "POST",
        f"/{PAPERS}/runs",
        {"input": INPUT, "files": [{"name": "a.txt", "content": "@@@"}]},
    )
    assert not_base64.status_code == 422
    assert not_base64.json()["detail"] == "a.txt: its content isn't base64"
    xml = runtime.world.client.post(
        f"/api/v1/runtime/workflows/{PAPERS}/runs",
        content=b"<run/>",
        headers={"Content-Type": "text/xml"},
    )
    assert xml.status_code == 415
    assert runtime.queue.queued == []


def test_files_answer_they_arent_set_up_where_nothing_keeps_them(
    runtime: Runtime,
) -> None:
    runtime.world.client.app.state.workflow_artifacts = None  # type: ignore[attr-defined]
    refused = upload(runtime, [("report.pdf", PDF, "application/pdf")])
    assert refused.status_code == 503
    assert "FORGE_ADMIN_WORKFLOW_ARTIFACTS" in refused.json()["detail"]
    # A run without files doesn't need them.
    assert runtime.start(PAPERS).status_code == 201


# ------------------------------------------------------------------ A2A


def send_parts(runtime: Runtime, *parts: dict[str, Any]) -> dict[str, Any]:
    message = {
        "messageId": str(uuid.uuid4()),
        "role": "ROLE_USER",
        "parts": list(parts),
    }
    return rpc(runtime, "SendMessage", {"message": message}, ref=PAPERS)


def test_a_task_s_file_parts_are_its_run_s_files(runtime: Runtime) -> None:
    task = send_parts(
        runtime,
        {"data": INPUT},
        {
            "raw": base64.b64encode(PDF).decode(),
            "filename": "report.pdf",
            "mediaType": "application/pdf",
        },
    )["result"]["task"]
    assert task["status"]["state"] == "TASK_STATE_WORKING", task
    run = runtime.call(runtime.runs.get, uuid.UUID(task["id"]).hex)
    assert [f["name"] for f in run["payload"]["files"]] == ["report.pdf"]
    assert kept(runtime, run, "report.pdf") == PDF


def test_a_task_s_files_are_held_to_the_start_and_never_fetched(
    runtime: Runtime,
) -> None:
    by_url = send_parts(
        runtime,
        {"data": INPUT},
        {"url": "https://example.com/r.pdf", "filename": "r.pdf"},
    )["result"]["task"]
    assert by_url["status"]["state"] == "TASK_STATE_REJECTED"
    assert (
        "send a file's bytes, not its URL"
        in by_url["status"]["message"]["parts"][0]["text"]
    )
    wrong_type = send_parts(
        runtime,
        {"data": INPUT},
        {"raw": base64.b64encode(b"x").decode(), "filename": "x.exe"},
    )["result"]["task"]
    assert wrong_type["status"]["state"] == "TASK_STATE_REJECTED"
    assert runtime.queue.queued == []


def test_a_workflow_s_card_says_what_files_it_takes(runtime: Runtime) -> None:
    card = runtime.world.call(
        "GET",
        f"/a2a/{PAPERS}/.well-known/agent-card.json",
        None,
        prefix="/api/v1/runtime",
    ).json()
    assert card["defaultInputModes"] == [
        "application/json",
        "text/plain",
        "application/pdf",
    ]
    [extension] = card["capabilities"]["extensions"]
    assert extension["params"]["files"] == {"allowed": True, "types": ["pdf", "text"]}
    assert (
        "Send its files as file parts (pdf, text)" in card["skills"][0]["description"]
    )


# ------------------------------------------------------------------ the format


def test_the_format_names_the_types_the_runner_knows(world: World) -> None:
    schema = json.loads(SCHEMA_PATH.read_text())
    types = schema["$defs"]["config_start"]["properties"]["file_types"]["items"]["enum"]
    assert types == list(FILE_TYPES)
