from pathlib import Path
from unittest.mock import Mock

import pytest

from forge_embeddings.vector_store import selected_backend, spanner_database


def test_backend_defaults_and_explicit_override(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("FORGE_VECTOR_STORE", raising=False)
    assert selected_backend() == "mongo"
    assert selected_backend("memory") == "memory"
    monkeypatch.setenv("FORGE_VECTOR_STORE", " SPANNER ")
    assert selected_backend("memory") == "spanner"
    monkeypatch.setenv("FORGE_VECTOR_STORE", "typo")
    with pytest.raises(ValueError, match="FORGE_VECTOR_STORE"):
        selected_backend()


def test_backend_dotenv_common_and_process_precedence(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("FORGE_VECTOR_STORE", raising=False)
    common = tmp_path / "common.env"
    common.write_text("FORGE_VECTOR_STORE=spanner\n")
    monkeypatch.setenv("FORGE_ENV_COMMON_FILE", str(common))
    Path(".env").write_text("FORGE_VECTOR_STORE=${FORGE_VECTOR_STORE}\n")
    assert selected_backend() == "spanner"
    monkeypatch.setenv("FORGE_VECTOR_STORE", "mongo")
    assert selected_backend() == "mongo"


def test_spanner_requires_explicit_database(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("FORGE_VECTOR_SPANNER_DATABASE", raising=False)
    with pytest.raises(ValueError, match="FORGE_VECTOR_SPANNER_DATABASE"):
        spanner_database()
    monkeypatch.setenv("FORGE_VECTOR_SPANNER_DATABASE", "projects/p/instances/i/databases/d")
    assert spanner_database() == "projects/p/instances/i/databases/d"


@pytest.mark.parametrize(
    "code,retry",
    [
        ("ServiceUnavailable", True),
        ("ResourceExhausted", True),
        ("Aborted", True),
        ("InvalidArgument", False),
        ("PermissionDenied", False),
    ],
)
async def test_spanner_error_classification(code, retry):
    from google.api_core import exceptions

    from forge_embeddings.vector_store.spanner import storage_io
    from forge_tasks.errors import TaskError

    def fail():
        raise getattr(exceptions, code)("test failure")

    with pytest.raises(TaskError) as error:
        await storage_io(fail)
    assert error.value.permanent is not retry


def test_spanner_managed_credentials_and_empty_emulator(monkeypatch):
    import json
    import os

    from google.oauth2 import service_account

    from forge_embeddings.vector_store import spanner as storage

    monkeypatch.setenv("FORGE_VECTOR_SPANNER_DATABASE", "projects/p/instances/i/databases/d")
    monkeypatch.setenv("SPANNER_EMULATOR_HOST", "")
    info = {"type": "service_account", "client_email": "worker@example.test"}
    monkeypatch.setenv("FORGE_GOOGLE_CREDENTIALS_JSON", json.dumps(info))
    credentials = Mock()
    loader = Mock(return_value=credentials)
    client = Mock()
    monkeypatch.setattr(service_account.Credentials, "from_service_account_info", loader)
    monkeypatch.setattr(storage.spanner, "Client", client)
    database = storage.Database()
    assert "SPANNER_EMULATOR_HOST" not in os.environ
    loader.assert_called_once_with(info)
    client.assert_called_once_with(project="p", credentials=credentials)
    assert database.db is client.return_value.instance.return_value.database.return_value


@pytest.mark.parametrize("value", ["secret-not-json", "[]", '{"type":"authorized_user"}'])
def test_spanner_rejects_invalid_credentials_without_exposing_them(monkeypatch, value):
    from forge_embeddings.vector_store.spanner import Database

    monkeypatch.setenv("FORGE_VECTOR_SPANNER_DATABASE", "projects/p/instances/i/databases/d")
    monkeypatch.setenv("FORGE_GOOGLE_CREDENTIALS_JSON", value)
    with pytest.raises(ValueError, match="valid service-account credentials") as error:
        Database()
    assert value not in str(error.value)
