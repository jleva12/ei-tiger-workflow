import json
import logging
from collections.abc import Iterator

import pytest
import structlog
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from forge_common.logging import LoggingSettings, configure_logging, get_logger
from forge_common.middleware import REQUEST_ID_HEADER, RequestContextMiddleware


@pytest.fixture(autouse=True)
def restore_logging() -> Iterator[None]:
    """configure_logging replaces the root logger's handlers, pytest's among them."""
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)
    for name in ("uvicorn.access", "httpx"):
        logging.getLogger(name).disabled = False
        logging.getLogger(name).setLevel(logging.NOTSET)
    structlog.reset_defaults()
    structlog.contextvars.clear_contextvars()


def json_lines(out: str) -> list[dict]:
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def test_levels_are_case_insensitive() -> None:
    settings = LoggingSettings.model_validate({"level": "debug", "levels": {"saq": "warning"}})

    assert settings.level == "DEBUG"
    assert settings.levels == {"saq": "WARNING"}


def test_format_follows_the_terminal_unless_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdout.isatty", lambda: False)
    assert LoggingSettings().resolved_format == "json"
    assert LoggingSettings(format="console").resolved_format == "console"

    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    assert LoggingSettings().resolved_format == "console"
    assert LoggingSettings(format="json").resolved_format == "json"


def test_json_lines_for_structlog_and_stdlib_loggers(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(LoggingSettings(format="json"))
    structlog.contextvars.bind_contextvars(request_id="r-1")

    get_logger("forge.test").info("order.created", order_id=7)
    logging.getLogger("library").warning("retrying %s in %ds", "job", 5, extra={"attempt": 2})

    structured, stdlib = json_lines(capsys.readouterr().out)
    assert structured["event"] == "order.created"
    assert structured["order_id"] == 7
    assert structured["level"] == "info"
    assert structured["logger"] == "forge.test"
    assert structured["request_id"] == "r-1"
    assert {"timestamp", "module", "lineno"} <= structured.keys()
    assert stdlib["event"] == "retrying job in 5s"
    assert stdlib["level"] == "warning"
    assert stdlib["logger"] == "library"
    assert stdlib["attempt"] == 2
    assert stdlib["request_id"] == "r-1"


def test_exceptions_render_as_structured_tracebacks(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(LoggingSettings(format="json"))

    try:
        raise ValueError("boom")
    except ValueError:
        logging.getLogger("library").exception("failed")

    (line,) = json_lines(capsys.readouterr().out)
    assert line["exception"][0]["exc_type"] == "ValueError"


def test_level_and_per_logger_overrides(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(LoggingSettings(format="json", level="WARNING", levels={"httpx": "ERROR"}))

    logging.getLogger("app").info("hidden")
    logging.getLogger("app").warning("shown")
    logging.getLogger("httpx").warning("hidden too")

    assert [line["event"] for line in json_lines(capsys.readouterr().out)] == ["shown"]


def test_console_format(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(LoggingSettings(format="console"))

    get_logger("forge.test").info("order.created", order_id=7)

    out = capsys.readouterr().out
    assert "order.created" in out
    assert "order_id=7" in out
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)


async def ok(request: Request) -> PlainTextResponse:
    get_logger("forge.test").info("handled")
    return PlainTextResponse("ok")


async def boom(request: Request) -> PlainTextResponse:
    raise RuntimeError("boom")


def client(*, access_log: bool = True, exclude_paths: list[str] | None = None) -> TestClient:
    # As the apps add it: their first middleware.
    middleware = Middleware(
        RequestContextMiddleware, access_log=access_log, exclude_paths=exclude_paths
    )
    app = Starlette(
        routes=[Route("/ok", ok), Route("/boom", boom), Route("/health", ok)],
        middleware=[middleware],
    )
    return TestClient(app, raise_server_exceptions=False)


def test_request_id_is_bound_to_every_line_and_returned(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging(LoggingSettings(format="json"))

    response = client().get("/ok")

    request_id = response.headers[REQUEST_ID_HEADER]
    handled, access = json_lines(capsys.readouterr().out)
    assert handled["event"] == "handled"
    assert handled["request_id"] == request_id
    assert access["event"] == "http.request"
    assert access["logger"] == "forge.access"
    assert access["request_id"] == request_id
    assert access["method"] == "GET"
    assert access["path"] == "/ok"
    assert access["status"] == 200
    assert access["level"] == "info"
    assert "duration_ms" in access


def test_caller_request_ids_are_kept_only_when_they_look_like_ids() -> None:
    configure_logging(LoggingSettings(format="json"))

    kept = client().get("/ok", headers={REQUEST_ID_HEADER: "abc-123"})
    replaced = client().get("/ok", headers={REQUEST_ID_HEADER: "bad id\nforged=1"})

    assert kept.headers[REQUEST_ID_HEADER] == "abc-123"
    assert replaced.headers[REQUEST_ID_HEADER] not in ("", "bad id\nforged=1")


def test_unhandled_exceptions_are_logged_once_and_answer_500(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging(LoggingSettings(format="json"))

    response = client().get("/boom")

    request_id = response.headers[REQUEST_ID_HEADER]
    assert response.status_code == 500
    assert response.json() == {"detail": "Internal Server Error", "request_id": request_id}
    error, access = json_lines(capsys.readouterr().out)
    assert error["event"] == "http.unhandled_exception"
    assert error["exception"][0]["exc_type"] == "RuntimeError"
    assert access["status"] == 500
    assert access["level"] == "error"


def test_access_log_skips_excluded_paths_and_can_be_off(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging(LoggingSettings(format="json"))

    client(exclude_paths=["/health"]).get("/health")
    client(access_log=False).get("/ok")

    events = [line["event"] for line in json_lines(capsys.readouterr().out)]
    assert events == ["handled", "handled"]
