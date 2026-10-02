"""The .env file and its ${NAME} references to the shared .env.common."""

import os
from pathlib import Path

import pytest
from pydantic import BaseModel, SecretStr
from pydantic_settings import SettingsError

from forge_tasks.env_files import COMMON_FILE_VARIABLE
from forge_tasks.settings import CoreSettings, load_section


class Section(BaseModel):
    """A task's settings section, like HYBRID_ADK_WORKFLOWS__*."""

    session_database_url: SecretStr | None = None
    google_api_key: SecretStr | None = None


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A checkout whose apps/forge-async-worker is the working directory, as for make async-worker."""
    app = tmp_path / "apps" / "forge-async-worker"
    app.mkdir(parents=True)
    monkeypatch.chdir(app)
    for name in list(os.environ):
        if name.startswith(("FORGE_", "HYBRID_")) or name in {"OPENAI_API_KEY", "ANTHROPIC_API_KEY"}:
            monkeypatch.delenv(name)
    return tmp_path


def write(path: Path, *lines: str) -> None:
    path.write_text("".join(f"{line}\n" for line in lines))


def test_references_take_shared_values_and_nested_settings_receive_them(root: Path) -> None:
    write(
        root / ".env.common",
        "FORGE_MONGO_USER=forge",
        "FORGE_MONGO_PASSWORD=shared-secret",
        "FORGE_MONGO_PORT=27027",
        "FORGE_REDIS_URL=redis://127.0.0.1:16379/0",
        "FORGE_GOOGLE_API_KEY=shared-google-key",
        "FORGE_MYSQL_PASSWORD=not-for-the-worker-unless-referenced",
    )
    write(
        root / "apps/forge-async-worker/.env",
        "HYBRID_REDIS_URL=${FORGE_REDIS_URL}",
        "HYBRID_ADK_WORKFLOWS__GOOGLE_API_KEY=${FORGE_GOOGLE_API_KEY}",
        "HYBRID_MONGO__URI=mongodb://${FORGE_MONGO_USER}:${FORGE_MONGO_PASSWORD}@127.0.0.1:${FORGE_MONGO_PORT}/?authSource=admin",
    )
    settings = CoreSettings()
    assert settings.redis_url == "redis://127.0.0.1:16379/0"
    assert settings.mongo.uri.get_secret_value() == "mongodb://forge:shared-secret@127.0.0.1:27027/?authSource=admin"
    section = load_section("adk_workflows", Section)
    assert section.google_api_key is not None
    assert section.google_api_key.get_secret_value() == "shared-google-key"
    # Nothing in .env.common reaches the settings or the environment by itself.
    assert section.session_database_url is None
    assert "FORGE_MYSQL_PASSWORD" not in os.environ


def test_earlier_lines_win_and_single_quotes_are_literal(root: Path) -> None:
    write(root / ".env.common", "FORGE_REDIS_URL=redis://shared:6379/0")
    write(
        root / "apps/forge-async-worker/.env",
        "FORGE_REDIS_URL=redis://mine:6379/1",
        'HYBRID_REDIS_URL="${FORGE_REDIS_URL}"',
        "HYBRID_MONGO__DATABASE='${FORGE_REDIS_URL}'",
    )
    settings = CoreSettings()
    assert settings.redis_url == "redis://mine:6379/1"
    assert settings.mongo.database == "${FORGE_REDIS_URL}"


def test_a_reference_defined_nowhere_stops_startup_unless_the_environment_sets_it(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(root / "apps/forge-async-worker/.env", "HYBRID_ADK_WORKFLOWS__GOOGLE_API_KEY=${FORGE_GOOGLE_API_KEY}")
    with pytest.raises(SettingsError) as error:
        load_section("adk_workflows", Section)
    assert str(error.value) == (
        "HYBRID_ADK_WORKFLOWS__GOOGLE_API_KEY in .env references ${FORGE_GOOGLE_API_KEY}, "
        "which neither .env.common nor an earlier line defines (make env creates .env.common)"
    )
    monkeypatch.setenv("HYBRID_ADK_WORKFLOWS__GOOGLE_API_KEY", "from-compose")
    assert load_section("adk_workflows", Section).google_api_key is not None


def test_the_common_file_variable_selects_disables_or_must_exist(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write(root / ".env.common", "FORGE_REDIS_URL=redis://default:6379/0")
    write(root / "other.env", "FORGE_REDIS_URL=redis://other:6379/0")
    write(root / "apps/forge-async-worker/.env", "HYBRID_REDIS_URL=${FORGE_REDIS_URL}")
    monkeypatch.setenv(COMMON_FILE_VARIABLE, str(root / "other.env"))
    assert CoreSettings().redis_url == "redis://other:6379/0"
    monkeypatch.setenv(COMMON_FILE_VARIABLE, "")
    with pytest.raises(SettingsError, match="references"):
        CoreSettings()
    monkeypatch.setenv(COMMON_FILE_VARIABLE, str(root / "missing.env"))
    with pytest.raises(SettingsError, match="doesn't exist"):
        CoreSettings()


def test_without_a_common_file_a_plain_env_file_works_as_before(root: Path) -> None:
    write(root / "apps/forge-async-worker/.env", "HYBRID_REDIS_URL=redis://plain:6379/0")
    assert CoreSettings().redis_url == "redis://plain:6379/0"
    assert CoreSettings(_env_file=None).redis_url == "redis://localhost:6379/0"  # type: ignore[call-arg]
