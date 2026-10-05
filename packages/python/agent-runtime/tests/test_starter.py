import copy
import io
import json
import zipfile
from pathlib import Path
from typing import Any

import pytest

from forge_agent_runtime.cli import main as cli
from forge_agent_runtime.starter import PyPI, Wheels, generate_project
from tests.conftest import EXAMPLE


def stated(example: dict[str, Any]) -> dict[str, Any]:
    doc = copy.deepcopy(example)
    doc["version"] = 3
    doc["nodes"][0]["config"]["state_schema"] = {
        "type": "object",
        "properties": {
            "customer_tier": {"type": "string", "enum": ["free", "pro"], "description": "Their plan"},
            "seats": {"type": "integer"},
        },
    }
    orders = next(n for n in doc["nodes"] if n["id"] == "orders")
    orders["config"]["headers"].append(
        {"id": "h", "name": "Authorization", "value": "Bearer ${ORDERS_TOKEN}"}
    )
    return doc


def text(project: Any, path: str) -> str:
    return project.files[path].decode()


def test_a_published_version_becomes_a_whole_project(example):
    project = generate_project(stated(example), runtime=PyPI("0.1.4"))
    assert project.name == "support-assistant"
    for path in (
        "main.py",
        "pyproject.toml",
        "README.md",
        "Dockerfile",
        "Makefile",
        ".gitignore",
        ".dockerignore",
        ".env.example",
        "model_provider.yaml",
        "agent/support-assistant.chat-agent.json",
        "web/package.json",
        "web/vite.config.ts",
        "web/components.json",
        "web/index.html",
        "web/src/App.tsx",
        "web/src/agent.tsx",
        "web/src/agent-state.ts",
        "web/src/index.css",
        "web/src/components/forge/assistant/index.ts",
        "web/src/components/forge/assistant/assistant-modal.tsx",
    ):
        assert path in project.files, path
    for path, content in project.files.items():
        assert b"@@" not in content, path

    main = text(project, "main.py")
    compile(main, "main.py", "exec")
    assert '.with_agent(HERE / "agent/support-assistant.chat-agent.json")' in main
    assert "version 3" in main
    assert '"forge-agent-runtime[server]>=0.1.4,<0.2"' in text(project, "pyproject.toml")
    assert "[tool.uv.sources]" not in text(project, "pyproject.toml")

    agent = json.loads(text(project, "agent/support-assistant.chat-agent.json"))
    assert (agent["id"], agent["version"]) == ("ca_1k2cuqsuev", 3)

    package = json.loads(text(project, "web/package.json"))
    assert {"react", "@assistant-ui/react-google-adk", "@base-ui/react"} <= set(package["dependencies"])
    assert "@forge-ui" in json.loads(text(project, "web/components.json"))["registries"]
    state = text(project, "web/src/agent-state.ts")
    assert '"customer_tier"?: "free" | "pro"' in state and "/** Their plan */" in state
    assert '"seats"?: number' in state


def test_env_example_asks_for_what_the_models_and_tools_need(example):
    project = generate_project(stated(example), runtime=PyPI("0.1.0"))
    env = text(project, ".env.example")
    assert "OPENAI_API_KEY=" in env and "OPENAI_MODEL=" in env
    assert "# Used by Look up order\nORDERS_TOKEN=" in env
    # Its models are the default provider's: Anthropic's is left out, and its key with it.
    assert "ANTHROPIC_API_KEY" not in env
    assert "\nNAME=" not in env
    assert "anthropic" not in text(project, "model_provider.yaml")
    # It still reads as forge-common reads it (its thinking levels' "off" stays text).
    from forge_common.model_provider import parse_model_provider_yaml

    config = parse_model_provider_yaml(
        text(project, "model_provider.yaml"), {"OPENAI_API_KEY": "sk", "OPENAI_MODEL": "gpt-5.6-sol"}
    )
    assert list(config.providers) == ["openai"]
    assert "`ORDERS_TOKEN`: for Look up order" in text(project, "README.md")


def test_the_wheels_come_along_until_the_runtime_is_on_pypi(tmp_path, example):
    for name in ("forge_agent_runtime-0.1.0", "forge_common-0.1.0", "forge_jsonata-0.1.0"):
        (tmp_path / f"{name}-py3-none-any.whl").write_bytes(b"wheel")
    project = generate_project(stated(example), runtime=Wheels(tmp_path))
    assert project.files["vendor/forge_common-0.1.0-py3-none-any.whl"] == b"wheel"
    pyproject = text(project, "pyproject.toml")
    assert 'forge-common = { path = "vendor/forge_common-0.1.0-py3-none-any.whl" }' in pyproject
    assert '"forge-jsonata",' in pyproject
    (tmp_path / "forge_jsonata-0.1.0-py3-none-any.whl").unlink()
    with pytest.raises(FileNotFoundError, match="forge_jsonata"):
        generate_project(stated(example), runtime=Wheels(tmp_path))


def test_a_draft_and_what_only_forge_runs_are_said(example):
    doc = stated(example)
    doc["version"] = "draft"
    doc["nodes"].append(
        {"id": "wf", "kind": "adk_workflow", "name": "Triage", "config": {"workflow": "ag_1"}, "outputs": []}
    )
    doc["edges"].append({"id": "w", "source": "agent", "source_output": "tools", "target": "wf"})
    project = generate_project(doc, runtime=PyPI("0.1.0"))
    assert any("draft" in note for note in project.notes)
    assert any("Triage runs a workflow" in note for note in project.notes)
    assert "# .with_services(workflows=...)  # Triage runs a workflow" in text(project, "main.py")
    readme = text(project, "README.md")
    assert "**A draft.**" in readme and "What needs more than this project" in readme
    compile(text(project, "main.py"), "main.py", "exec")


def test_the_zip_holds_the_project_in_a_folder(example):
    project = generate_project(stated(example), name="Shop helper", runtime=PyPI("0.1.0"))
    with zipfile.ZipFile(io.BytesIO(project.zip())) as archive:
        names = archive.namelist()
        assert "shop-helper/main.py" in names
        assert archive.read("shop-helper/agent/shop-helper.chat-agent.json")


def test_the_cli_writes_a_project(tmp_path, capsys):
    out = tmp_path / "helper"
    assert cli(["new", str(EXAMPLE), "--out", str(out)]) == 0
    assert (out / "main.py").is_file() and (out / "web" / "src" / "agent.tsx").is_file()
    assert "Next:" in capsys.readouterr().out
    assert cli(["new", str(EXAMPLE), "--out", str(out)]) == 1
    assert "is taken" in capsys.readouterr().err
    assert cli(["new", str(EXAMPLE), "--out", str(out), "--zip"]) == 0
    assert Path(f"{out}.zip").is_file()


def test_every_choice_shapes_the_project(example):
    from forge_agent_runtime.starter import StarterOptions

    options = StarterOptions(
        interface="api",
        sessions="postgresql",
        artifacts="s3",
        memory="atlas",
        streaming=False,
        api_key=True,
        cors_origins=["https://app.example.com/"],
        code_owners=["@acme/agents", "ada"],
    )
    project = generate_project(stated(example), runtime=PyPI("0.1.0"), options=options)
    for path, content in project.files.items():
        assert b"@@" not in content, path
    assert not any(path.startswith("web/") for path in project.files)

    main = text(project, "main.py")
    compile(main, "main.py", "exec")
    assert "from forge_agent_runtime import AgentServer, env" in main
    for line in (
        '.with_sessions(env("DATABASE_URL"))',
        '.with_artifacts(env("ARTIFACTS_URL"))',
        '.with_memory(env("MONGODB_URI"))',
        ".with_streaming(False)",
        '.with_cors("https://app.example.com")',
        '.with_api_keys(*env("AGENT_API_KEYS").split(","))',
    ):
        assert f"\t{line}\n" in main, line
    assert ".with_web" not in main

    pyproject = text(project, "pyproject.toml")
    assert (
        '"forge-agent-runtime[server,sessions-postgres,artifacts-s3,memory-atlas]>=0.1.0,<0.2"' in pyproject
    )

    import yaml

    compose = yaml.safe_load(text(project, "compose.yaml"))
    assert sorted(compose["services"]) == ["mongo", "postgres", "s3"]
    env = text(project, ".env.example")
    assert "DATABASE_URL=postgresql://agent:agent@127.0.0.1:5432/agent" in env
    assert "ARTIFACTS_URL=s3://agent-files/support-assistant" in env
    assert "FORGE_AGENT_ARTIFACTS_CREATE_BUCKET=true" in env
    assert "MONGODB_URI=mongodb://127.0.0.1:27017/?directConnection=true" in env
    assert project.env[-1] == "AGENT_API_KEYS" and "OPENAI_API_KEY" in project.env
    assert "DATABASE_URL" not in project.env  # filled in for compose.yaml's

    docker = text(project, "Dockerfile")
    assert "FROM node" not in docker and "web/dist" not in docker and "COPY vendor/" not in docker
    makefile = text(project, "Makefile")
    assert ".PHONY: install services api run docker" in makefile and "\nweb:" not in makefile
    assert text(project, ".github/CODEOWNERS").endswith("* @acme/agents @ada\n")
    agents = text(project, "AGENTS.md")
    assert "PostgreSQL" in agents and "web/" not in agents.split("## Layout")[1].split("## Commands")[0]
    assert text(project, "CLAUDE.md") == "@AGENTS.md\n"
    assert "agent/chat_agent.schema.json" in project.files
    readme = text(project, "README.md")
    assert "| Replies | Whole, once the model is done |" in readme
    assert "-H 'authorization: Bearer <key>'" in readme and "docker compose up -d --wait" in readme


def test_sqlite_and_a_folder_keep_things_in_data(example):
    from forge_agent_runtime.starter import StarterOptions

    options = StarterOptions(sessions="sqlite", artifacts="folder")
    project = generate_project(stated(example), runtime=PyPI("0.1.0"), options=options)
    main = text(project, "main.py")
    assert "sqlite:///{HERE / 'data' / 'sessions.db'}" in main
    assert '.with_artifacts(env("ARTIFACTS_DIR", str(HERE / "data" / "artifacts")))' in main
    assert ".with_web(" in main and ".with_streaming(True)" in main
    assert "compose.yaml" not in project.files
    assert "VOLUME /app/data" in text(project, "Dockerfile")
    assert "FROM node" in text(project, "Dockerfile")
    assert '"forge-agent-runtime[server]>=0.1.0,<0.2"' in text(project, "pyproject.toml")


def test_the_defaults_keep_everything_in_memory_with_the_ui(example):
    project = generate_project(stated(example), runtime=PyPI("0.1.0"))
    main = text(project, "main.py")
    assert "from forge_agent_runtime import AgentServer\n" in main
    for line in ('.with_sessions("memory")', '.with_artifacts("memory")', '.with_memory("memory")'):
        assert line in main
    assert "# .with_auth(Depends(verify_token))" in main
    assert text(project, ".github/CODEOWNERS").splitlines()[-1] == "# /agent/ @your-org/agent-owners"
    assert "compose.yaml" not in project.files and "VOLUME" not in text(project, "Dockerfile")
    assert "web/src/agent.tsx" in project.files


def test_options_refuse_what_cant_work():
    from pydantic import ValidationError

    from forge_agent_runtime.starter import StarterOptions

    with pytest.raises(ValidationError, match="can't keep an API key secret"):
        StarterOptions(api_key=True)
    with pytest.raises(ValidationError, match="isn't an origin"):
        StarterOptions(interface="api", cors_origins=["https://app.example.com/path"])
    with pytest.raises(ValidationError, match="isn't a GitHub user"):
        StarterOptions(code_owners=["@acme/agents team"])
    assert StarterOptions(code_owners=[" ada ", "", "ops@acme.com", "@ada"]).code_owners == [
        "@ada",
        "ops@acme.com",
    ]


def test_atlas_memory_without_a_memory_tool_is_said(example):
    from forge_agent_runtime.starter import StarterOptions

    doc = stated(example)
    doc["nodes"] = [n for n in doc["nodes"] if n["kind"] != "memory"]
    doc["edges"] = [e for e in doc["edges"] if e["target"] in {n["id"] for n in doc["nodes"]}]
    project = generate_project(doc, runtime=PyPI("0.1.0"), options=StarterOptions(memory="atlas"))
    assert any("no Memory tool" in note for note in project.notes)


def test_template_conditions():
    from forge_agent_runtime.starter import _conditional

    template = "a\n@@if ui@@\nb\n@@if !compose@@\nc\n@@end@@\n@@end@@\nd @@if api@@e@@end@@f\n"
    assert _conditional(template, {"ui"}) == "a\nb\nc\nd f\n"
    assert _conditional(template, {"ui", "compose", "api"}) == "a\nb\nd ef\n"
    with pytest.raises(ValueError, match="no @@end@@"):
        _conditional("@@if ui@@\nx\n", {"ui"})


def test_the_cli_takes_the_choices(tmp_path, capsys):
    out = tmp_path / "api"
    flags = ["--api-only", "--api-key", "--sessions", "mysql", "--artifacts", "s3", "--no-streaming"]
    assert cli(["new", str(EXAMPLE), "--out", str(out), *flags, "--code-owner", "@acme/agents"]) == 0
    assert '.with_sessions(env("DATABASE_URL"))' in (out / "main.py").read_text()
    assert not (out / "web").exists() and (out / "compose.yaml").is_file()
    assert cli(["new", str(EXAMPLE), "--out", str(tmp_path / "ui"), "--api-key"]) == 1
    assert "can't keep an API key secret" in capsys.readouterr().err
