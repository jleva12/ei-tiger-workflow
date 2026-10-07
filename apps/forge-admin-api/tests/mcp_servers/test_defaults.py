"""The application's default MCP servers (mcp_servers/default_servers.yaml)."""

from pathlib import Path

import pytest

from forge_admin.mcp_servers import defaults


def test_the_bundled_servers_load_with_the_code_explorer() -> None:
    servers = {server.id: server for server in defaults.load(environment={})}
    explorer = servers["code-explorer"]
    assert explorer.name == "Code explorer"
    # Natively the code graph MCP server listens on 8103.
    assert explorer.url == "http://localhost:8103/mcp"
    assert explorer.auth.kind == "bearer"
    assert explorer.auth.fields["token"].label == "Organization API key"
    assert explorer.instructions and explorer.headers == []


def test_references_resolve_from_the_environment() -> None:
    url = "http://codegraph-mcp:8103/mcp"
    servers = defaults.load(environment={"FORGE_ADMIN_CODEGRAPH_MCP_URL": url})
    assert next(s for s in servers if s.id == "code-explorer").url == url


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "servers.yaml"
    path.write_text(text)
    return path


SERVER = """
servers:
  - id: tickets
    name: Tickets
    url: https://mcp.example.com/mcp
    auth:
      kind: api_key
      settings: {header: X-Tickets-Key}
      fields:
        key: {description: From the tickets admin page.}
    headers:
      - {name: X-Tenant, required: true, description: Your tenant.}
"""


def test_a_server_with_settings_field_help_and_headers(tmp_path: Path) -> None:
    [server] = defaults.load(write(tmp_path, SERVER))
    assert server.auth.settings == {"header": "X-Tickets-Key"}
    assert server.auth.fields["key"].description == "From the tickets admin page."
    assert server.headers[0].required and server.timeout_seconds == 30


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        (("kind: api_key", "kind: smoke_signals"), "no auth method 'smoke_signals'"),
        (("header: X-Tickets-Key", "colour: blue"), "colour"),
        (("key: {description", "password: {description"), "has no field password"),
        (("https://mcp.example.com/mcp", "${TICKETS_URL}"), "TICKETS_URL"),
        (("https://mcp.example.com/mcp", "ftp://example.com"), "url"),
        (("name: X-Tenant", "name: X Tenant"), "name"),
    ],
)
def test_a_bad_server_stops_the_api(
    tmp_path: Path, change: tuple[str, str], problem: str
) -> None:
    with pytest.raises(ValueError, match=problem):
        defaults.load(write(tmp_path, SERVER.replace(*change)))


def test_ids_are_unique(tmp_path: Path) -> None:
    twice = SERVER + SERVER.split("servers:", 1)[1].replace("name: Tickets", "name: T2")
    with pytest.raises(ValueError, match="ids must be unique: tickets"):
        defaults.load(write(tmp_path, twice))


def test_a_fallback_is_used_when_the_value_is_unset(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        SERVER.replace(
            "https://mcp.example.com/mcp", "${TICKETS_URL:-http://localhost:9/mcp}"
        ),
    )
    assert defaults.load(path, environment={})[0].url == "http://localhost:9/mcp"
    assert (
        defaults.load(path, environment={"TICKETS_URL": "https://t.example/mcp"})[0].url
        == "https://t.example/mcp"
    )
