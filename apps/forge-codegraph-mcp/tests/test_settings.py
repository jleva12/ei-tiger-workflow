from pathlib import Path

import pytest
from conftest import CURSOR_KEY, make_settings
from pydantic import ValidationError

from forge_codegraph_mcp.core.settings import Settings


def test_environment_and_nesting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEGRAPH_SPANNER__CURSOR_SIGNING_KEY", CURSOR_KEY)
    monkeypatch.setenv("CODEGRAPH_SPANNER__SCOPE", "staging")
    monkeypatch.setenv("CODEGRAPH_SERVER__PORT", "9000")
    monkeypatch.setenv("CODEGRAPH_MCP__AUTH__ADMIN_URL", "http://admin:8091/api/v1")
    monkeypatch.setenv("CODEGRAPH_MCP__AUTH__ADMIN_API_KEY", " ")
    monkeypatch.setenv("CODEGRAPH_EMBEDDING__DIMENSIONS", "1024")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.spanner.scope == "staging" and settings.server.port == 9000
    assert settings.mcp.auth.admin_url == "http://admin:8091/api/v1"
    # A blank deployment key is none.
    assert settings.mcp.auth.admin_api_key is None
    assert settings.embedding.vector_length == 1024 and not settings.embedding.enabled


def test_defaults() -> None:
    settings = make_settings()
    assert settings.server.port == 8103
    assert settings.mcp.path == "/mcp" and settings.mcp.stateless_http
    assert settings.mcp.instructions and "list_repositories" in settings.mcp.instructions
    assert settings.embedding.vector_length == 3072
    assert settings.spanner.database.endswith("/databases/codegraph")
    # The admin API as make admin serves it, its answers kept a minute.
    assert settings.mcp.auth.admin_url == "http://localhost:8101/api/v1"
    assert settings.mcp.auth.cache_seconds == 60


def test_cursor_key_is_required() -> None:
    with pytest.raises(ValidationError, match="cursor_signing_key is required"):
        make_settings(spanner={"cursor_signing_key": None})
    with pytest.raises(ValidationError, match="at least 32"):
        make_settings(spanner={"cursor_signing_key": "short"})


def test_invalid_values() -> None:
    with pytest.raises(ValidationError, match=r"spanner\.database"):
        make_settings(spanner={"database": "codegraph"})
    with pytest.raises(ValidationError, match=r"mcp\.auth\.admin_url"):
        make_settings(mcp={"auth": {"admin_url": "admin:8091"}})
    with pytest.raises(ValidationError, match="mutually exclusive"):
        make_settings(server={"reload": True, "workers": 2})


def test_embedding_needs_a_model_and_a_key() -> None:
    settings = make_settings(embedding={"model": "text-embedding-3-large", "api_key": "sk-x"})
    assert settings.embedding.enabled
    assert not make_settings(embedding={"model": "text-embedding-3-large"}).embedding.enabled


def test_env_file_references_env_common(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    common = tmp_path / ".env.common"
    common.write_text(
        f"FORGE_CODEGRAPH_CURSOR_SIGNING_KEY={CURSOR_KEY}\n"
        "FORGE_EMBEDDING_DIMENSIONS=1024\n"
        "OPENAI_API_KEY=sk-test\n"
        "UNREFERENCED=nope\n"
    )
    env = tmp_path / ".env"
    env.write_text(
        "CODEGRAPH_SPANNER__CURSOR_SIGNING_KEY=${FORGE_CODEGRAPH_CURSOR_SIGNING_KEY}\n"
        "CODEGRAPH_EMBEDDING__DIMENSIONS=${FORGE_EMBEDDING_DIMENSIONS}\n"
        "CODEGRAPH_EMBEDDING__API_KEY=${OPENAI_API_KEY}\n"
        "CODEGRAPH_EMBEDDING__MODEL=text-embedding-3-large\n"
    )
    monkeypatch.setenv("FORGE_ENV_COMMON_FILE", str(common))
    settings = Settings(_env_file=env)  # type: ignore[call-arg]
    assert settings.spanner.cursor_signing_key is not None
    assert settings.spanner.cursor_signing_key.get_secret_value() == CURSOR_KEY
    assert settings.embedding.dimensions == 1024 and settings.embedding.enabled


def test_the_templates_embed_with_the_shared_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """.env.example, resolved against .env.common.example, embeds with the
    model, dimensions and gateway every app shares."""
    root = Path(__file__).resolve().parents[3]
    common = tmp_path / ".env.common"
    common.write_text(
        (root / ".env.common.example")
        .read_text()
        .replace(
            "FORGE_CODEGRAPH_CURSOR_SIGNING_KEY=",
            f"FORGE_CODEGRAPH_CURSOR_SIGNING_KEY={CURSOR_KEY}",
        )
        .replace("OPENAI_API_KEY=", "OPENAI_API_KEY=sk-test")
    )
    monkeypatch.setenv("FORGE_ENV_COMMON_FILE", str(common))
    # make env copies the admin API's token key in.
    monkeypatch.setenv("CODEGRAPH_MCP__AUTH__PUBLIC_KEY", "s" * 40)
    settings = Settings(_env_file=root / "apps/forge-codegraph-mcp/.env.example")  # type: ignore[call-arg]
    shared = dict(
        line.split("=", 1)
        for line in common.read_text().splitlines()
        if line.startswith("FORGE_EMBEDDING_")
    )
    assert settings.embedding.model == shared["FORGE_EMBEDDING_MODEL"]
    assert settings.embedding.dimensions == int(shared["FORGE_EMBEDDING_DIMENSIONS"])
    assert not settings.embedding.base_url and settings.embedding.enabled
