import pytest
from pydantic import ValidationError
from sqlalchemy import make_url

from forge_admin.config import Settings


def test_database_url_escapes_credentials() -> None:
    password = "p@ss:w/rd#1?"
    settings = Settings(
        _env_file=None,
        mysql_host="mysql.internal",
        mysql_port=3306,
        mysql_database="forge_admin",
        mysql_user="forge_admin",
        mysql_password=password,
    )
    url = settings.database_url
    rendered = make_url(url.render_as_string(hide_password=False))
    assert rendered.password == password
    assert rendered.host == "mysql.internal"
    assert rendered.drivername == "mysql+aiomysql"
    assert rendered.query["charset"] == "utf8mb4"
    # Logging the URL never shows the password.
    assert password not in str(url)


def test_settings_read_the_prefixed_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FORGE_ADMIN_MYSQL_HOST", "mysql.internal")
    monkeypatch.setenv("FORGE_ADMIN_MYSQL_PASSWORD", "secret")
    monkeypatch.setenv("FORGE_ADMIN_CORS_ORIGINS", '["http://localhost:5180"]')
    monkeypatch.setenv("FORGE_ADMIN_API_KEY", "distinctive-secret")
    settings = Settings(_env_file=None)  # pyright: ignore[reportCallIssue]
    assert settings.mysql_host == "mysql.internal"
    assert settings.cors_origins == ["http://localhost:5180"]
    assert settings.api_key is not None
    assert "distinctive-secret" not in repr(settings)


def test_blank_api_key_disables_the_check() -> None:
    settings = Settings(_env_file=None, mysql_password="x", api_key="")
    assert settings.api_key is None


def test_blank_optional_paths_are_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    # As .env.example writes them: an empty path would be "." (a directory),
    # which the assistant's screens loader can't read.
    monkeypatch.setenv("FORGE_ADMIN_AGENT_SCREENS", "")
    monkeypatch.setenv("FORGE_ADMIN_MODEL_PROVIDER_CONFIG", "")
    settings = Settings(_env_file=None, mysql_password="x")  # pyright: ignore[reportCallIssue]
    assert settings.agent_screens is None
    assert settings.model_provider_config is None


def test_mysql_password_has_no_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FORGE_ADMIN_MYSQL_PASSWORD", raising=False)
    with pytest.raises(ValidationError, match="mysql_password"):
        Settings(_env_file=None)  # pyright: ignore[reportCallIssue]
