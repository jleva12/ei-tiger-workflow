import pytest
from fastapi.testclient import TestClient
from google.adk.artifacts import FileArtifactService, InMemoryArtifactService
from google.adk.memory import InMemoryMemoryService
from google.adk.sessions import DatabaseSessionService

from forge_agent_runtime import AgentServer, MissingSetting, env
from forge_agent_runtime.app import artifact_service, database_url
from tests.conftest import text
from tests.test_app import AGENT, session, settings


def run(client: TestClient, streaming: bool) -> str:
    reply = client.post(
        "/api/run_sse",
        json={
            "appName": AGENT,
            "userId": "ada",
            "sessionId": session(client),
            "newMessage": {"role": "user", "parts": [{"text": "hi"}]},
            "streaming": streaming,
        },
    )
    assert reply.status_code == 200, reply.text
    return reply.text


@pytest.mark.parametrize(
    ("server_says", "asked", "partial"),
    [(None, True, True), (None, False, False), (False, True, False), (True, False, True)],
)
def test_replies_stream_as_the_server_says_or_as_asked(agent_file, models, llm, server_says, asked, partial):
    server = AgentServer(settings()).with_agent(agent_file).with_models(models)
    server.with_streaming(server_says)
    llm.turns = [[text("Hello there, Ada.")]]
    with TestClient(server.build()) as client:
        events = run(client, asked)
    assert '"partial":true' in events.replace(" ", "") if partial else "partial" not in events
    assert "Ada." in events


def test_api_keys_guard_the_api(agent_file, models, llm):
    server = AgentServer(settings()).with_agent(agent_file).with_models(models)
    server.with_api_keys("k-one", " ", "k-two")
    with TestClient(server.build()) as client:
        assert client.get("/api/agent").status_code == 401
        assert client.get("/api/agent", headers={"Authorization": "Bearer nope"}).status_code == 401
        assert client.get("/api/agent", headers={"Authorization": "Bearer k-two"}).status_code == 200
        assert client.get("/api/agent", headers={"X-API-Key": "k-one"}).status_code == 200
        assert client.get("/healthz").status_code == 200


def test_where_things_are_kept_comes_from_settings_or_methods(tmp_path, agent_file, models):
    server = AgentServer(
        settings(
            sessions=f"sqlite:///{tmp_path}/data/sessions.db",
            artifacts=str(tmp_path / "files"),
        )
    )
    executor = server.with_agent(agent_file).with_models(models).executor
    assert isinstance(executor.sessions, DatabaseSessionService)
    assert (tmp_path / "data").is_dir()  # SQLite's folder is made
    assert isinstance(executor.artifacts, FileArtifactService)
    assert isinstance(executor.memory, InMemoryMemoryService)
    assert executor.streaming is None
    assert "files " + str(tmp_path / "files") in server._kept()
    assert "conversations sqlite+aiosqlite:///" in server._kept()

    plain = AgentServer(settings()).with_agent(agent_file).with_models(models)
    plain.with_artifacts("memory").with_streaming(False)
    assert isinstance(plain.executor.artifacts, InMemoryArtifactService)
    assert plain.executor.streaming is False
    assert plain._kept() == ("conversations in memory; files in memory; memory in memory; replies whole")


def test_memory_is_in_memory_or_atlas(agent_file, models):
    server = AgentServer(settings()).with_agent(agent_file).with_models(models)
    server.with_memory("mongodb+srv://u:p@cluster.example.net")
    assert server.settings.memory_atlas_uri is not None
    assert "memory MongoDB Atlas" in server._kept()
    server.with_memory("memory")
    assert server.settings.memory_atlas_uri is None
    with pytest.raises(ValueError, match="MongoDB Atlas"):
        server.with_memory("redis://localhost")


def test_database_urls_as_hosts_hand_them_out_get_async_drivers():
    assert database_url("postgres://u:p@db:5432/x?sslmode=require") == (
        "postgresql+asyncpg://u:p@db:5432/x?ssl=require"
    )
    assert database_url("postgresql://db/x") == "postgresql+asyncpg://db/x"
    assert database_url("mysql://db/x") == "mysql+aiomysql://db/x"
    assert database_url("sqlite:///data/s.db") == "sqlite+aiosqlite:///data/s.db"
    assert database_url("postgresql+psycopg://db/x") == "postgresql+psycopg://db/x"
    assert database_url("memory") == "memory"


def test_artifacts_are_kept_in_memory_a_folder_or_s3(tmp_path):
    assert isinstance(artifact_service("memory", {}), InMemoryArtifactService)
    folder = artifact_service(f"file://{tmp_path}/files", {})
    assert isinstance(folder, FileArtifactService) and (tmp_path / "files").is_dir()
    from forge_common.adk.artifacts import S3ArtifactService

    s3 = artifact_service("s3://bucket/agent", {"AWS_REGION": "us-east-1"})
    assert isinstance(s3, S3ArtifactService)
    assert (s3.bucket, s3.prefix) == ("bucket", "agent/")
    with pytest.raises(ValueError, match="gs://"):
        artifact_service("gs://bucket", {})


def test_env_reads_the_environment_then_dotenv(tmp_path, monkeypatch):
    dotenv = tmp_path / ".env"
    dotenv.write_text("DATABASE_URL=sqlite:///from-file.db\nBLANK=\n")
    assert env("DATABASE_URL", env_file=dotenv) == "sqlite:///from-file.db"
    monkeypatch.setenv("DATABASE_URL", "postgresql://from-env")
    assert env("DATABASE_URL", env_file=dotenv) == "postgresql://from-env"
    assert env("BLANK", "fallback", env_file=dotenv) == "fallback"
    with pytest.raises(MissingSetting, match="ARTIFACTS_URL isn't set"):
        env("ARTIFACTS_URL", env_file=dotenv)


def test_a_folder_of_artifacts_keeps_a_saved_file(tmp_path):
    import asyncio

    from google.genai import types

    files = artifact_service(str(tmp_path / "files"), {})
    saved = asyncio.run(
        files.save_artifact(
            app_name="a", user_id="u", session_id="s", filename="n.txt", artifact=types.Part(text="x")
        )
    )
    assert saved == 0
    assert [p.name for p in (tmp_path / "files").rglob("*") if p.is_file()]
