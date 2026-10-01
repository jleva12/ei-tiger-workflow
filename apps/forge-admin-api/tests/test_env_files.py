"""The .env file and its ${NAME} references to the shared .env.common."""

import os
from pathlib import Path

import pytest
from pydantic_settings import SettingsError

from forge_admin.config import Settings
from forge_admin.env_files import COMMON_FILE_VARIABLE

UNDEFINED = (
    "FORGE_ADMIN_WORKFLOWS_TOKEN in .env references ${FORGE_WORKFLOWS_TOKEN}, "
    "which neither .env.common nor an earlier line defines "
    "(make env creates .env.common)"
)


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A checkout whose apps/forge-admin-api is the working directory, as for make admin."""
    app = tmp_path / "apps" / "forge-admin-api"
    app.mkdir(parents=True)
    monkeypatch.chdir(app)
    monkeypatch.delenv(COMMON_FILE_VARIABLE, raising=False)
    for name in list(os.environ):
        if name.upper().startswith("FORGE_"):
            monkeypatch.delenv(name)
    return tmp_path


def write(path: Path, *lines: str) -> None:
    path.write_text("".join(f"{line}\n" for line in lines))


def load() -> Settings:
    return Settings()  # pyright: ignore[reportCallIssue]


def test_a_reference_takes_the_common_value(root: Path) -> None:
    write(
        root / ".env.common",
        "FORGE_MYSQL_HOST=mysql.shared",
        "FORGE_MYSQL_PORT=13316",
        "FORGE_MYSQL_PASSWORD=shared-secret",
        # The shared file may reference its own earlier lines.
        "FORGE_ASYNC_WORKER_HOST=worker.shared",
        "FORGE_ASYNC_WORKER_URL=http://${FORGE_ASYNC_WORKER_HOST}:8094",
    )
    write(
        root / "apps/forge-admin-api/.env",
        "FORGE_ADMIN_MYSQL_HOST=${FORGE_MYSQL_HOST}",
        "FORGE_ADMIN_MYSQL_PORT=${FORGE_MYSQL_PORT}",
        'FORGE_ADMIN_MYSQL_PASSWORD="${FORGE_MYSQL_PASSWORD}"',
        "FORGE_ADMIN_ASYNC_WORKER_URL=${FORGE_ASYNC_WORKER_URL}",
        "FORGE_ADMIN_WEB_URL=http://${FORGE_MYSQL_HOST}:${FORGE_MYSQL_PORT}/console",
    )
    settings = load()
    assert settings.mysql_host == "mysql.shared"
    assert settings.mysql_port == 13316
    assert settings.mysql_password.get_secret_value() == "shared-secret"
    assert settings.async_worker_url == "http://worker.shared:8094"
    assert settings.web_url == "http://mysql.shared:13316/console"


def test_an_earlier_line_overrides_the_common_value(root: Path) -> None:
    write(
        root / ".env.common",
        "FORGE_MYSQL_HOST=mysql.shared",
        "FORGE_MYSQL_USER=shared_user",
    )
    write(
        root / "apps/forge-admin-api/.env",
        "FORGE_MYSQL_HOST=mysql.local",
        "FORGE_ADMIN_MYSQL_HOST=${FORGE_MYSQL_HOST}",
        # Only earlier lines count.
        "FORGE_ADMIN_MYSQL_USER=${FORGE_MYSQL_USER}",
        "FORGE_MYSQL_USER=later_user",
        "FORGE_ADMIN_MYSQL_PASSWORD=x",
    )
    settings = load()
    assert settings.mysql_host == "mysql.local"
    assert settings.mysql_user == "shared_user"


def test_a_single_quoted_value_is_literal(root: Path) -> None:
    write(root / ".env.common", "FORGE_MYSQL_PASSWORD=shared-secret")
    write(
        root / "apps/forge-admin-api/.env",
        "FORGE_ADMIN_MYSQL_PASSWORD='${FORGE_MYSQL_PASSWORD}'",
        "export FORGE_ADMIN_JWT_ISSUER = '${UNDEFINED}'",
    )
    settings = load()
    assert settings.mysql_password.get_secret_value() == "${FORGE_MYSQL_PASSWORD}"
    assert settings.jwt_issuer == "${UNDEFINED}"


def test_a_reference_defined_nowhere_stops_startup(root: Path) -> None:
    write(root / ".env.common", "FORGE_MYSQL_PASSWORD=shared-secret")
    write(
        root / "apps/forge-admin-api/.env",
        "FORGE_ADMIN_MYSQL_PASSWORD=${FORGE_MYSQL_PASSWORD}",
        "FORGE_ADMIN_WORKFLOWS_TOKEN=${FORGE_WORKFLOWS_TOKEN}",
    )
    with pytest.raises(SettingsError) as error:
        load()
    # Names the variable, the file and the reference; never a value.
    assert str(error.value) == UNDEFINED
    assert error.value.__cause__ is None and error.value.__context__ is None


def test_the_process_environment_takes_no_part_in_references(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FORGE_WORKFLOWS_TOKEN", "t" * 40)
    write(
        root / "apps/forge-admin-api/.env",
        "FORGE_ADMIN_MYSQL_PASSWORD=x",
        "FORGE_ADMIN_WORKFLOWS_TOKEN=${FORGE_WORKFLOWS_TOKEN}",
    )
    with pytest.raises(SettingsError) as error:
        load()
    assert str(error.value) == UNDEFINED


def test_a_reference_defined_nowhere_is_ignored_when_the_environment_sets_it(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = "t" * 40
    monkeypatch.setenv("FORGE_ADMIN_WORKFLOWS_TOKEN", key)
    write(
        root / "apps/forge-admin-api/.env",
        "FORGE_ADMIN_MYSQL_PASSWORD=x",
        "FORGE_ADMIN_WORKFLOWS_TOKEN=${FORGE_WORKFLOWS_TOKEN}",
    )
    settings = load()
    assert settings.workflows_token is not None
    assert settings.workflows_token.get_secret_value() == key


def test_an_unreferenced_common_value_reaches_nothing(root: Path) -> None:
    write(
        root / ".env.common",
        "FORGE_MYSQL_PASSWORD=shared-secret",
        "FORGE_ADMIN_API_KEY=shared-api-key",
        "FORGE_ADMIN_MYSQL_HOST=mysql.shared",
    )
    write(
        root / "apps/forge-admin-api/.env",
        "FORGE_ADMIN_MYSQL_PASSWORD=${FORGE_MYSQL_PASSWORD}",
    )
    settings = load()
    assert settings.api_key is None
    assert settings.mysql_host == "127.0.0.1"
    for name in (
        "FORGE_MYSQL_PASSWORD",
        "FORGE_ADMIN_API_KEY",
        "FORGE_ADMIN_MYSQL_HOST",
    ):
        assert name not in os.environ


def test_an_empty_common_value_is_defined(root: Path) -> None:
    write(root / ".env.common", "FORGE_WORKFLOWS_TOKEN=")
    write(
        root / "apps/forge-admin-api/.env",
        "FORGE_ADMIN_MYSQL_PASSWORD=x",
        "FORGE_ADMIN_WORKFLOWS_TOKEN=${FORGE_WORKFLOWS_TOKEN}",
    )
    # Blank, as before make env generates the token: unset.
    assert load().workflows_token is None


def test_precedence_is_unchanged(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write(root / ".env.common", "FORGE_MYSQL_HOST=mysql.shared")
    write(
        root / "apps/forge-admin-api/.env",
        "FORGE_ADMIN_MYSQL_HOST=${FORGE_MYSQL_HOST}",
        "FORGE_ADMIN_MYSQL_USER=${FORGE_MYSQL_HOST}",
        "FORGE_ADMIN_MYSQL_DATABASE=${FORGE_MYSQL_HOST}",
        "FORGE_ADMIN_MYSQL_PASSWORD=x",
    )
    monkeypatch.setenv("FORGE_ADMIN_MYSQL_HOST", "mysql.environment")
    monkeypatch.setenv("FORGE_ADMIN_MYSQL_USER", "environment_user")
    settings = Settings(mysql_user="init_user")  # pyright: ignore[reportCallIssue]
    assert settings.mysql_host == "mysql.environment"
    assert settings.mysql_user == "init_user"
    assert settings.mysql_database == "mysql.shared"


def test_an_empty_common_file_variable_disables_it(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(COMMON_FILE_VARIABLE, "")
    write(root / ".env.common", "FORGE_MYSQL_PASSWORD=shared-secret")
    write(
        root / "apps/forge-admin-api/.env",
        "FORGE_ADMIN_MYSQL_PASSWORD=${FORGE_MYSQL_PASSWORD}",
    )
    with pytest.raises(SettingsError) as error:
        load()
    assert str(error.value) == (
        "FORGE_ADMIN_MYSQL_PASSWORD in .env references ${FORGE_MYSQL_PASSWORD}, "
        "which no earlier line defines (FORGE_ENV_COMMON_FILE is empty)"
    )
    write(root / "apps/forge-admin-api/.env", "FORGE_ADMIN_MYSQL_PASSWORD=local")
    assert load().mysql_password.get_secret_value() == "local"


def test_a_missing_common_file_stops_startup(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = root / "missing.env"
    monkeypatch.setenv(COMMON_FILE_VARIABLE, str(missing))
    monkeypatch.setenv("FORGE_ADMIN_MYSQL_PASSWORD", "x")
    # Even without a .env to resolve.
    with pytest.raises(SettingsError) as error:
        load()
    assert (
        str(error.value)
        == f"FORGE_ENV_COMMON_FILE names {missing}, which doesn't exist"
    )


def test_the_common_file_variable_names_another_file(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shared = root / "shared.env"
    write(shared, "FORGE_MYSQL_PASSWORD=from-shared")
    write(root / ".env.common", "FORGE_MYSQL_PASSWORD=from-common")
    monkeypatch.setenv(COMMON_FILE_VARIABLE, str(shared))
    write(
        root / "apps/forge-admin-api/.env",
        "FORGE_ADMIN_MYSQL_PASSWORD=${FORGE_MYSQL_PASSWORD}",
        "FORGE_ADMIN_WORKFLOWS_TOKEN=${FORGE_WORKFLOWS_TOKEN}",
    )
    with pytest.raises(SettingsError) as error:
        load()
    assert str(error.value) == (
        "FORGE_ADMIN_WORKFLOWS_TOKEN in .env references ${FORGE_WORKFLOWS_TOKEN}, "
        f"which neither {shared} nor an earlier line defines"
    )
    write(
        root / "apps/forge-admin-api/.env",
        "FORGE_ADMIN_MYSQL_PASSWORD=${FORGE_MYSQL_PASSWORD}",
    )
    assert load().mysql_password.get_secret_value() == "from-shared"


def test_a_common_reference_defined_nowhere_stops_startup(root: Path) -> None:
    write(
        root / ".env.common",
        "FORGE_MYSQL_PASSWORD=shared-secret",
        "FORGE_S3_ENDPOINT_URL=http://${FORGE_S3_HOST}:9000",
    )
    write(
        root / "apps/forge-admin-api/.env",
        "FORGE_ADMIN_MYSQL_PASSWORD=${FORGE_MYSQL_PASSWORD}",
    )
    with pytest.raises(SettingsError) as error:
        load()
    assert str(error.value) == (
        "FORGE_S3_ENDPOINT_URL in ../../.env.common references ${FORGE_S3_HOST}, "
        "which no earlier line defines"
    )


def test_without_a_common_file_a_plain_env_file_works_as_before(root: Path) -> None:
    write(
        root / "apps/forge-admin-api/.env",
        "# The admin's own settings.",
        "FORGE_ADMIN_MYSQL_HOST=mysql.local",
        'FORGE_ADMIN_MYSQL_PASSWORD="p@ss $word"',
        "FORGE_ADMIN_CORS_ORIGINS=["
        '"http://localhost:5180","http://localhost:18180"]  # comment',
    )
    settings = load()
    assert settings.mysql_host == "mysql.local"
    assert settings.mysql_password.get_secret_value() == "p@ss $word"
    assert settings.cors_origins == ["http://localhost:5180", "http://localhost:18180"]


def test_an_env_file_argument_resolves_references_too(root: Path) -> None:
    write(root / ".env.common", "FORGE_MYSQL_PASSWORD=shared-secret")
    custom = root / "custom.env"
    write(custom, "FORGE_ADMIN_MYSQL_PASSWORD=${FORGE_MYSQL_PASSWORD}")
    settings = Settings(_env_file=custom)  # pyright: ignore[reportCallIssue]
    assert settings.mysql_password.get_secret_value() == "shared-secret"


def test_no_env_file_reads_no_common_file(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(COMMON_FILE_VARIABLE, str(root / "missing.env"))
    write(root / "apps/forge-admin-api/.env", "FORGE_ADMIN_MYSQL_HOST=${UNDEFINED}")
    settings = Settings(_env_file=None, mysql_password="x")
    assert settings.mysql_host == "127.0.0.1"
