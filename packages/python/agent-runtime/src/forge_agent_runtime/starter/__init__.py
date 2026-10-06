"""A chat agent as a project of its own, as Spring Initializr makes a Spring Boot app.

:func:`generate_project` writes everything an exported agent needs to run
anywhere, made of what :class:`StarterOptions` picks (where it keeps
conversations, files and memories, how it replies, whether it has a UI, who
may call it, who owns it):

- ``main.py``: an :class:`~forge_agent_runtime.AgentServer` around the agent,
  one ``with_*`` line per choice;
- ``agent/<name>.chat-agent.json``: the agent, as exported, and its JSON Schema;
- ``model_provider.yaml``: the models it may run on (only the providers it
  uses), its keys as ``${NAME}``s; and ``.env.example`` naming every one, the
  agent's tools' and the choices';
- ``web/`` (with the UI): a Vite + React UI made of Forge's components (copied
  in from the ``@forge-ui`` registry: ``web_vendor``), a blank page with the
  assistant in its corner;
- ``compose.yaml``: the databases and stores the choices need, for development;
- ``pyproject.toml`` (with the runtime's wheels in ``vendor/`` until it's on
  PyPI), a ``Dockerfile``, a ``Makefile``, a ``README.md``, an ``AGENTS.md``
  for AI coding agents, and ``.github/CODEOWNERS``.

Templates are files under ``template/``: ``@@NAME@@`` placeholders, and
``@@if flag@@`` … ``@@end@@`` for what only some projects have (a line of its
own, or inline)::

    project = generate_project(load_document("support.chat-agent.json"), runtime=Wheels(Path("dist")))
    project.write(Path("support-assistant"))     # or project.zip()

``forge-agent new agent.json`` does the same from the command line.
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from importlib import metadata, resources
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Any

from forge_agent_runtime.document import ChatAgentDocument, parse_document
from forge_agent_runtime.names import adk_name
from forge_agent_runtime.starter.options import ARTIFACTS, MEMORY, SESSIONS, StarterOptions

__all__ = [
    "PyPI",
    "StarterOptions",
    "StarterProject",
    "Wheels",
    "generate_project",
    "project_name",
]

#: The wheels a project installs while the runtime isn't on PyPI.
WHEEL_DISTRIBUTIONS = ("forge_agent_runtime", "forge_common", "forge_jsonata")
#: ``${NAME}``: a value from the environment (``$${NAME}`` is literal text).
REFERENCE = re.compile(r"(?<!\$)\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
#: Templates kept without their leading dot, which packaging can drop.
DOTFILES = {"gitignore": ".gitignore", "dockerignore": ".dockerignore"}
#: Templates that would be read as the package's own (pyproject.toml, main.py) end in this.
TEMPLATE_SUFFIX = ".template"
#: ``@@if flag@@ … @@end@@`` on one line, and on lines of their own.
INLINE_IF = re.compile(r"@@if (!?[a-z0-9-]+)@@(.*?)@@end@@")
BLOCK_IF = re.compile(r"@@if (!?[a-z0-9-]+)@@")
REACT = "^19.2.8"
#: Nodes only a hosted runtime can run, and the service a project gives them.
HOSTED_ONLY = {
    "adk_workflow": ("workflows", "runs a workflow"),
    "knowledge_base": ("knowledge_bases", "searches knowledge bases"),
}
#: Development services: what .env.example says, matching compose.yaml.
DEV_DATABASE = {
    "postgresql": "postgresql://agent:agent@127.0.0.1:5432/agent",
    "mysql": "mysql://agent:agent@127.0.0.1:3306/agent",
}
DEV_S3 = {
    "AWS_ACCESS_KEY_ID": "agent",
    "AWS_SECRET_ACCESS_KEY": "agent-secret",
    "AWS_REGION": "us-east-1",
}
DEV_S3_ENDPOINT = "http://127.0.0.1:9000"
DEV_MONGODB = "mongodb://127.0.0.1:27017/?directConnection=true"


@dataclass(frozen=True)
class Wheels:
    """The runtime's wheels in a folder (``uv build``'s dist), copied into ``vendor/``."""

    directory: Path

    def find(self) -> list[Path]:
        """
        :return: The newest wheel of each distribution the project needs.
        :raises FileNotFoundError: One of them isn't there.
        """
        found, missing = [], []
        for dist in WHEEL_DISTRIBUTIONS:
            wheels = sorted(self.directory.glob(f"{dist}-*.whl"), key=lambda p: p.stat().st_mtime)
            if wheels:
                found.append(wheels[-1])
            else:
                missing.append(dist)
        if missing:
            raise FileNotFoundError(
                f"No {', '.join(missing)} wheel in {self.directory}: build them with uv build."
            )
        return found


@dataclass(frozen=True)
class PyPI:
    """The runtime from PyPI, at this version (and its compatible updates)."""

    version: str


@dataclass
class StarterProject:
    """
    A generated project: its folder name, its files by path, what to know
    about it, and what its ``.env`` needs filled in.
    """

    name: str
    files: dict[str, bytes]
    notes: list[str] = field(default_factory=list)
    #: The settings .env.example leaves blank: the models' keys, the tools' secrets, API keys.
    env: list[str] = field(default_factory=list)

    def zip(self) -> bytes:
        """:return: The project as a zip, its files in a folder named after it."""
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for path, content in sorted(self.files.items()):
                info = zipfile.ZipInfo(f"{self.name}/{path}", date_time=(2026, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                archive.writestr(info, content)
        return buffer.getvalue()

    def write(self, directory: Path) -> Path:
        """Writes the files into ``directory`` (made if missing). :return: It."""
        for path, content in self.files.items():
            target = directory / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        return directory


def project_name(name: str) -> str:
    """A project's folder and package name from an agent's: ``Support assistant``: ``support-assistant``."""
    return adk_name(name).replace("_", "-") or "agent"


def _runtime_version() -> str:
    try:
        return metadata.version("forge-agent-runtime")
    except metadata.PackageNotFoundError:
        return "0.1.0"


def _package() -> Traversable:
    return resources.files("forge_agent_runtime")


def _template() -> Traversable:
    return _package().joinpath("starter")


def _walk(folder: Traversable, prefix: str = "") -> Iterator[tuple[str, bytes]]:
    for entry in sorted(folder.iterdir(), key=lambda e: e.name):
        if entry.name == "__pycache__":
            continue
        path = f"{prefix}{entry.name}"
        if entry.is_dir():
            yield from _walk(entry, f"{path}/")
        else:
            yield path, entry.read_bytes()


def _conditional(text: str, flags: set[str]) -> str:
    """Keeps what ``@@if flag@@`` … ``@@end@@`` keep for these flags (``!flag``: without it)."""

    def keep(condition: str) -> bool:
        return condition[1:] not in flags if condition.startswith("!") else condition in flags

    text = INLINE_IF.sub(lambda m: m.group(2) if keep(m.group(1)) else "", text)
    out: list[str] = []
    kept: list[bool] = []
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        opened = BLOCK_IF.fullmatch(stripped)
        if opened:
            kept.append(keep(opened.group(1)))
        elif stripped == "@@end@@":
            kept.pop()
        elif all(kept):
            out.append(line)
    if kept:
        raise ValueError("A template's @@if@@ has no @@end@@")
    return "".join(out)


def _documents(doc: ChatAgentDocument) -> Iterator[dict[str, Any]]:
    """The agent's document and those of the saved agents it carries, as JSON."""
    yield doc.to_json()
    yield from doc.dependencies.values()


def _nodes(doc: ChatAgentDocument) -> Iterator[dict[str, Any]]:
    for document in _documents(doc):
        yield from document.get("nodes") or []


def _tool_references(doc: ChatAgentDocument) -> dict[str, str]:
    """The ``${NAME}``s the agent's tools use, each with the first tool that does."""
    out: dict[str, str] = {}
    for node in _nodes(doc):
        if node.get("kind") not in ("http_tool", "openapi", "mcp"):
            continue
        for name in REFERENCE.findall(json.dumps(node.get("config") or {})):
            out.setdefault(name, str(node.get("name") or node.get("id")))
    return out


def _hosted_only(doc: ChatAgentDocument) -> list[tuple[str, str, str]]:
    """``(node, service, what it does)`` for each node only a hosted runtime can run."""
    out = []
    for node in _nodes(doc):
        kind, config = node.get("kind"), node.get("config") or {}
        name = str(node.get("name") or node.get("id"))
        if kind in HOSTED_ONLY:
            service, what = HOSTED_ONLY[kind]
            out.append((name, service, what))
        elif kind == "mcp" and config.get("server"):
            out.append((name, "mcp_servers", "uses one of the organization's MCP servers"))
    return out


def _models_used(doc: ChatAgentDocument) -> set[str]:
    """The providers the agent's LLM agents pick a model of."""
    out = set()
    for node in _nodes(doc):
        model = (node.get("config") or {}).get("model")
        if isinstance(model, dict) and model.get("provider"):
            out.add(str(model["provider"]))
    return out


def _trimmed_config(text: str, providers: set[str]) -> str:
    """
    The model provider config with only the default provider and those the agent
    picks, so ``.env`` asks only for their keys. It's read as forge-common reads
    it (YAML 1.2: ``off`` is text, not false), and kept as it was unless the
    trimmed one reads back the same.
    """
    import yaml

    from forge_common.model_provider.config import _Yaml12Loader

    try:
        config = yaml.load(text, Loader=_Yaml12Loader)
        kept = {config["default"]["provider"], *providers}
        all_providers = config["providers"]
        config["providers"] = {key: value for key, value in all_providers.items() if key in kept}
    except (yaml.YAMLError, KeyError, TypeError):
        return text
    if len(config["providers"]) == len(all_providers):
        return text
    header = (
        "# The models this agent may run on: Forge's model provider configuration, with the\n"
        "# providers it doesn't use left out. Its keys are filled in from .env.\n"
    )
    trimmed = header + yaml.safe_dump(config, sort_keys=False, allow_unicode=True, width=100)
    return trimmed if yaml.load(trimmed, Loader=_Yaml12Loader) == config else text


def _has_provider(text: str, provider: str) -> bool:
    import yaml

    from forge_common.model_provider.config import _Yaml12Loader

    try:
        return provider in (yaml.load(text, Loader=_Yaml12Loader).get("providers") or {})
    except (yaml.YAMLError, AttributeError):
        return False


def _config_references(text: str) -> list[str]:
    """The ``${NAME}``s a YAML file's values use (its comments may show examples)."""
    values = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    return list(dict.fromkeys(REFERENCE.findall(values)))


def _typescript(schema: Mapping[str, Any]) -> str:
    """A JSON Schema as a TypeScript type, as far as one says (``unknown`` beyond)."""
    if "enum" in schema and all(isinstance(v, (str, int, float, bool)) for v in schema["enum"]):
        return " | ".join(json.dumps(v) for v in schema["enum"]) or "never"
    kind = schema.get("type")
    if isinstance(kind, list):
        return " | ".join(_typescript({**schema, "type": k}) for k in kind)
    if kind == "string":
        return "string"
    if kind in ("number", "integer"):
        return "number"
    if kind == "boolean":
        return "boolean"
    if kind == "null":
        return "null"
    if kind == "array":
        items = schema.get("items")
        return f"Array<{_typescript(items) if isinstance(items, dict) else 'unknown'}>"
    if kind == "object":
        properties = schema.get("properties")
        if isinstance(properties, dict) and properties:
            required = set(schema.get("required") or [])
            fields = "; ".join(
                f"{json.dumps(name)}{'' if name in required else '?'}: {_typescript(sub if isinstance(sub, dict) else {})}"
                for name, sub in properties.items()
            )
            return "{ " + fields + " }"
        return "Record<string, unknown>"
    return "unknown"


def _agent_state(doc: ChatAgentDocument) -> str:
    properties = doc.state_schema.get("properties")
    lines = []
    for name, sub in properties.items() if isinstance(properties, dict) else []:
        sub = sub if isinstance(sub, dict) else {}
        if sub.get("description"):
            lines.append(f"  /** {sub['description']} */")
        lines.append(f"  {json.dumps(name)}?: {_typescript(sub)}")
    return (
        "/**\n"
        f" * The state {doc.name} takes: what its input schema declares, sent with every\n"
        " * message as ADK's stateDelta, for its instructions to read as {{ state.<name> }}.\n"
        ' * Set it from your page, e.g. `agentState.customer_tier = "pro"`.\n'
        " */\n"
        "export type AgentState = {\n"
        "  /** The model to run on, provider/model; the agent's own when unset. */\n"
        "  model?: string\n"
        "  /** How long the model thinks: off, minimal, low, medium, high or xhigh. */\n"
        "  thinking_level?: string\n" + "".join(f"{line}\n" for line in lines) + "}\n\n"
        "export const agentState: AgentState = {}\n"
    )


def _chain(options: StarterOptions) -> tuple[str, bool]:
    """
    :return: ``main.py``'s ``with_*`` lines for the choices (tab-indented, as
        its template is), and whether they read settings with ``env``.
    """
    lines: list[str] = []

    def add(comment: str, call: str) -> None:
        lines.extend([f"\t# {comment}", f"\t{call}"])

    sessions = {
        "memory": ("Conversations: in memory, gone when it restarts.", '.with_sessions("memory")'),
        "sqlite": (
            "Conversations: in SQLite, in data/ (DATABASE_URL to keep them elsewhere).",
            ".with_sessions(env(\"DATABASE_URL\", f\"sqlite:///{HERE / 'data' / 'sessions.db'}\"))",
        ),
        "postgresql": (
            "Conversations: in PostgreSQL, at DATABASE_URL.",
            '.with_sessions(env("DATABASE_URL"))',
        ),
        "mysql": ("Conversations: in MySQL, at DATABASE_URL.", '.with_sessions(env("DATABASE_URL"))'),
    }
    add(*sessions[options.sessions])
    artifacts = {
        "memory": (
            "Files the agent's tools save: in memory, gone when it restarts.",
            '.with_artifacts("memory")',
        ),
        "folder": (
            "Files the agent's tools save: in data/artifacts (ARTIFACTS_DIR to keep them elsewhere).",
            '.with_artifacts(env("ARTIFACTS_DIR", str(HERE / "data" / "artifacts")))',
        ),
        "s3": (
            "Files the agent's tools save: in S3, at ARTIFACTS_URL (s3://bucket/prefix; AWS_* say as whom).",
            '.with_artifacts(env("ARTIFACTS_URL"))',
        ),
    }
    add(*artifacts[options.artifacts])
    memory = {
        "memory": (
            "Long-term memory (the Memory tool): in memory, gone when it restarts.",
            '.with_memory("memory")',
        ),
        "atlas": (
            "Long-term memory (the Memory tool): MongoDB Atlas at MONGODB_URI, searched by meaning.",
            '.with_memory(env("MONGODB_URI"))',
        ),
    }
    add(*memory[options.memory])
    if options.streaming:
        add("Replies stream as the model writes them.", ".with_streaming(True)")
    else:
        add("Replies arrive whole, not word by word.", ".with_streaming(False)")
    if options.a2a:
        add(
            "Google's A2A protocol too: JSON-RPC at /a2a, its card at /.well-known/agent-card.json.",
            ".with_a2a()",
        )
    else:
        lines += [
            "\t# Google's A2A protocol too (forge-agent-runtime[a2a]): JSON-RPC at /a2a:",
            "\t# .with_a2a()",
        ]
    if options.ui:
        add("The built UI, served at /; the run API is at /api.", '.with_web(HERE / "web" / "dist")')
    if options.cors_origins:
        add(
            "Pages elsewhere that may call the API.",
            f".with_cors({', '.join(json.dumps(o) for o in options.cors_origins)})",
        )
    elif not options.ui:
        lines += ["\t# Pages elsewhere that may call the API:", '\t# .with_cors("https://app.example.com")']
    if options.api_key:
        add(
            "Callers send one of these keys: Authorization: Bearer <key>.",
            '.with_api_keys(*env("AGENT_API_KEYS").split(","))',
        )
    elif not options.ui:
        lines += [
            "\t# Ask callers for a key (Authorization: Bearer <key>):",
            '\t# .with_api_keys(*env("AGENT_API_KEYS").split(","))',
        ]
    else:
        lines += [
            "\t# Ask callers to sign in (a FastAPI dependency):",
            "\t# .with_auth(Depends(verify_token))",
        ]
    uses_env = options.sessions != "memory" or options.artifacts != "memory" or options.memory != "memory"
    return "".join(f"{line}\n" for line in lines), uses_env or options.api_key


def _choices(options: StarterOptions) -> str:
    """The choices as a Markdown table's rows."""
    rows = [
        ("Conversations", *SESSIONS[options.sessions]),
        ("Files the agent's tools save", *ARTIFACTS[options.artifacts]),
        ("Long-term memory", *MEMORY[options.memory]),
    ]
    out = [f"| {what} | {label}: {where} |" for what, label, where in rows]
    out.append(
        "| Replies | Streamed as the model writes them |"
        if options.streaming
        else "| Replies | Whole, once the model is done |"
    )
    out.append(
        "| Interface | The chat UI and the API, on one port |"
        if options.ui
        else "| Interface | The API alone, for your own front end or services |"
    )
    out.append(
        "| Protocols | ADK's run API (`/api`) and Google's A2A (`/a2a`, 1.0 and 0.3) |"
        if options.a2a
        else "| Protocols | ADK's run API (`/api`) |"
    )
    access = "An API key (`AGENT_API_KEYS`)" if options.api_key else "Anyone who can reach it"
    if options.cors_origins:
        access += "; pages on " + ", ".join(f"`{o}`" for o in options.cors_origins)
    out.append(f"| Access | {access} |")
    return "\n".join(out)


def _env_example(
    model_names: list[str], tool_names: Mapping[str, str], options: StarterOptions, slug: str
) -> str:
    out = [
        "# Copy to .env and fill in. The server reads it as it starts.",
        "",
        "# --- The models (model_provider.yaml's ${NAME}s); each one is needed ---",
        *[f"{name}=" for name in model_names],
    ]
    if tool_names:
        out += ["", "# --- The agent's tools (${NAME}s in their URLs and headers) ---"]
        out += [f"# Used by {tool}\n{name}=" for name, tool in tool_names.items()]
    kept: list[str] = []
    if options.sessions in DEV_DATABASE:
        kept += [
            "# Conversations: compose.yaml's database (postgres:// and mysql:// URLs, as hosts give them, work)",
            f"DATABASE_URL={DEV_DATABASE[options.sessions]}",
        ]
    elif options.sessions == "sqlite":
        kept += [
            "# Conversations: SQLite, in data/ unless this says otherwise",
            "# DATABASE_URL=sqlite:///data/sessions.db",
        ]
    if options.artifacts == "s3":
        kept += [
            "# Files the agent's tools save: a bucket, and a prefix in it",
            f"ARTIFACTS_URL=s3://agent-files/{slug}",
            "# As whom (compose.yaml's S3 here; your own, or none for an IAM role, in production)",
            *[f"{key}={value}" for key, value in DEV_S3.items()],
            "# S3 that isn't Amazon's (compose.yaml's RustFS, MinIO, R2); remove it for Amazon S3",
            f"AWS_ENDPOINT_URL_S3={DEV_S3_ENDPOINT}",
            "# Make the bucket if it isn't there (compose.yaml's S3 starts empty); false in production",
            "FORGE_AGENT_ARTIFACTS_CREATE_BUCKET=true",
        ]
    elif options.artifacts == "folder":
        kept += [
            "# Files the agent's tools save: data/artifacts unless this says otherwise",
            "# ARTIFACTS_DIR=data/artifacts",
        ]
    if options.memory == "atlas":
        kept += [
            "# Long-term memory: compose.yaml's Atlas Local (mongodb+srv://… for Atlas itself)",
            f"MONGODB_URI={DEV_MONGODB}",
        ]
    ports = {
        "postgres": "POSTGRES_PORT=5432",
        "mysql": "MYSQL_PORT=3306",
        "s3": "S3_PORT=9000",
        "mongo": "MONGO_PORT=27017",
    }
    if options.services:
        kept += [
            "# compose.yaml's ports, if these are taken; change the addresses above to match",
            *[f"# {ports[service]}" for service in options.services],
        ]
    if kept:
        out += ["", "# --- Where it keeps things (main.py) ---", *kept]
    if options.api_key:
        out += [
            "",
            "# --- Access ---",
            "# Keys callers send (Authorization: Bearer <key>), comma-separated; make one with",
            "# openssl rand -hex 32",
            "AGENT_API_KEYS=",
        ]
    out += [
        "",
        "# --- The server (forge_agent_runtime.AgentServerSettings); these are the defaults ---",
        "# FORGE_AGENT_HOST=127.0.0.1",
        "# FORGE_AGENT_PORT=8000",
        *(
            [
                "# The address A2A's card gives callers; the one each request came to by default",
                "# FORGE_AGENT_PUBLIC_URL=https://agent.example.com",
            ]
            if options.a2a
            else []
        ),
        "# What HTTP tools may reach besides the internet",
        "# FORGE_AGENT_ALLOW_PRIVATE=false",
        "# FORGE_AGENT_ALLOWED_HOSTS=[]",
        "",
    ]
    return "\n".join(out)


def _compose(options: StarterOptions, name: str) -> str | None:
    """compose.yaml: the databases and stores the choices need, for development."""
    services = options.services
    if not services:
        return None
    out = [
        f"# What {name} keeps things in, for development: docker compose up -d --wait.",
        "# Its addresses and passwords are .env.example's; use your own in production.",
        "services:",
    ]
    volumes = []
    if "postgres" in services:
        volumes.append("postgres")
        out += [
            "  postgres:",
            "    image: postgres:17",
            "    environment:",
            "      POSTGRES_USER: agent",
            "      POSTGRES_PASSWORD: agent",
            "      POSTGRES_DB: agent",
            '    ports: ["127.0.0.1:${POSTGRES_PORT:-5432}:5432"]',
            "    volumes: [postgres:/var/lib/postgresql/data]",
            "    healthcheck:",
            '      test: ["CMD-SHELL", "pg_isready -U agent -d agent"]',
            "      interval: 2s",
            "      retries: 30",
        ]
    if "mysql" in services:
        volumes.append("mysql")
        out += [
            "  mysql:",
            "    image: mysql:8.4",
            "    environment:",
            "      MYSQL_USER: agent",
            "      MYSQL_PASSWORD: agent",
            "      MYSQL_DATABASE: agent",
            "      MYSQL_ROOT_PASSWORD: agent-root",
            '    ports: ["127.0.0.1:${MYSQL_PORT:-3306}:3306"]',
            "    volumes: [mysql:/var/lib/mysql]",
            "    healthcheck:",
            '      test: ["CMD", "mysqladmin", "ping", "-h", "127.0.0.1", "-uagent", "-pagent"]',
            "      interval: 2s",
            "      retries: 60",
        ]
    if "s3" in services:
        volumes.append("s3")
        out += [
            "  # S3 that runs locally (RustFS); the agent makes its bucket on first use.",
            "  s3:",
            "    image: rustfs/rustfs:1.0.0",
            "    environment:",
            f"      RUSTFS_ACCESS_KEY: {DEV_S3['AWS_ACCESS_KEY_ID']}",
            f"      RUSTFS_SECRET_KEY: {DEV_S3['AWS_SECRET_ACCESS_KEY']}",
            '    ports: ["127.0.0.1:${S3_PORT:-9000}:9000"]',
            "    volumes: [s3:/data]",
            "    healthcheck:",
            '      test: ["CMD", "curl", "--fail", "--silent", "http://127.0.0.1:9000/health/live"]',
            "      interval: 2s",
            "      retries: 30",
        ]
    if "mongo" in services:
        volumes += ["mongo-data", "mongo-config"]
        out += [
            "  # MongoDB with Atlas Search and Vector Search, as Atlas has them.",
            "  mongo:",
            "    image: mongodb/mongodb-atlas-local:8.3.9",
            "    hostname: mongo",
            "    environment:",
            '      DO_NOT_TRACK: "1"',
            '    ports: ["127.0.0.1:${MONGO_PORT:-27017}:27017"]',
            "    volumes: [mongo-data:/data/db, mongo-config:/data/configdb]",
            "    healthcheck:",
            '      test: ["CMD", "/usr/local/bin/runner", "healthcheck"]',
            "      interval: 5s",
            "      start_period: 60s",
            "      retries: 30",
        ]
    out += ["volumes:", *[f"  {volume}:" for volume in volumes], ""]
    return "\n".join(out)


def _code_owners(options: StarterOptions) -> str:
    header = (
        "# Who reviews changes: GitHub users (@name), teams (@org/team) or emails. Later\n"
        "# lines win. https://docs.github.com/articles/about-code-owners\n"
    )
    if options.code_owners:
        return header + f"* {' '.join(options.code_owners)}\n"
    return header + "# * @your-org/your-team\n# /agent/ @your-org/agent-owners\n"


def _components_json(config: Mapping[str, Any]) -> str:
    components = {
        "$schema": "https://ui.shadcn.com/schema.json",
        "style": config.get("style", "base-rhea"),
        "rsc": False,
        "tsx": True,
        "tailwind": {
            "config": "",
            "css": "src/index.css",
            "baseColor": "neutral",
            "cssVariables": True,
            "prefix": "",
        },
        "iconLibrary": config.get("iconLibrary", "hugeicons"),
        "rtl": False,
        "aliases": {
            "components": "@/components",
            "utils": "@/lib/utils",
            "ui": "@/components/ui",
            "lib": "@/lib",
            "hooks": "@/hooks",
        },
        "menuColor": config.get("menuColor", "inverted-translucent"),
        "menuAccent": config.get("menuAccent", "subtle"),
        # More of Forge's components: npx shadcn add @forge-ui/<name> (FORGE_UI_TOKEN in .env.local).
        "registries": config.get("registries", {}),
    }
    return json.dumps(components, indent=2) + "\n"


def _indented_json(value: Any, indent: str) -> str:
    return json.dumps(value, indent=2).replace("\n", f"\n{indent}")


def generate_project(
    document: ChatAgentDocument | Mapping[str, Any],
    *,
    name: str | None = None,
    model_provider_yaml: str | None = None,
    runtime: Wheels | PyPI | None = None,
    options: StarterOptions | None = None,
) -> StarterProject:
    """
    :param document: The agent: a published version, or a draft (``"version": "draft"``).
    :param name: The project's name; the agent's, as a slug, by default.
    :param model_provider_yaml: The model provider configuration; forge-common's
        OpenAI and Anthropic one by default.
    :param runtime: Where the project gets the runtime: wheels copied into it, or
        PyPI (this runtime's version by default).
    :param options: What it's made of; everything in memory, with the UI, by default.
    :return: The project.
    :raises FileNotFoundError: The wheels aren't built.
    """
    doc = document if isinstance(document, ChatAgentDocument) else parse_document(document)
    options = options or StarterOptions()
    slug = project_name(name or doc.name)
    runtime = runtime or PyPI(_runtime_version())
    if model_provider_yaml is None:
        from forge_common.model_provider import SHARED_CONFIG_DIR

        model_provider_yaml = (SHARED_CONFIG_DIR / "model_provider.openai.yaml").read_text("utf-8")
    providers = _models_used(doc)
    if options.memory == "atlas":
        # Atlas memory embeds with the OpenAI provider.
        providers.add("openai")
    model_provider_yaml = _trimmed_config(model_provider_yaml, providers)
    model_names = _config_references(model_provider_yaml)
    tool_names = _tool_references(doc)
    hosted = _hosted_only(doc)
    draft = doc.version == "draft"
    version_label = (
        "its draft" if draft else f"version {doc.version}" if isinstance(doc.version, int) else "as exported"
    )
    agent_file = f"agent/{slug}.chat-agent.json"
    notes: list[str] = []
    if draft:
        notes.append("It's the agent's draft: it may not build until it's finished.")
    for node, service, what in hosted:
        notes.append(
            f"{node} {what}, which only Forge's runtime has: give it one with .with_services({service}=…)."
        )
    kinds = {node.get("kind") for node in _nodes(doc)}
    if options.memory == "atlas" and "memory" not in kinds:
        notes.append("The agent has no Memory tool, so MongoDB Atlas memory isn't used until it gets one.")
    if options.memory == "atlas" and not _has_provider(model_provider_yaml, "openai"):
        notes.append(
            "MongoDB Atlas memory embeds with OpenAI: add an openai provider to model_provider.yaml."
        )

    files: dict[str, bytes] = {}
    extras = ",".join(options.extras)
    if isinstance(runtime, Wheels):
        wheels = runtime.find()
        for wheel in wheels:
            files[f"vendor/{wheel.name}"] = wheel.read_bytes()
        dependencies = [
            f'"forge-agent-runtime[{extras}]"',
            '"forge-common[adk,adk-models]"',
            '"forge-jsonata"',
        ]
        sources = "\n[tool.uv.sources]\n" + "".join(
            f'{wheel.name.split("-")[0].replace("_", "-")} = {{ path = "vendor/{wheel.name}" }}\n'
            for wheel in wheels
        )
    else:
        major, minor = (runtime.version.split(".") + ["0"])[:2]
        dependencies = [f'"forge-agent-runtime[{extras}]>={runtime.version},<{major}.{int(minor) + 1}"']
        sources = ""

    vendor = _template().joinpath("web_vendor")
    manifest = json.loads(vendor.joinpath("manifest.json").read_text("utf-8"))
    npm = {"react": REACT, "react-dom": REACT, **manifest["dependencies"]}
    services = "".join(
        f"\t# .with_services({service}=...)  # {node} {what}\n" for node, service, what in hosted
    )
    chain, uses_env = _chain(options)
    env_example = _env_example(model_names, tool_names, options, slug)
    needs = [
        line.removesuffix("=")
        for line in env_example.splitlines()
        if line.endswith("=") and not line.startswith("#")
    ]
    env_list = "".join(f"   - `{name}`: for model_provider.yaml\n" for name in model_names) + "".join(
        f"   - `{name}`: for {tool}\n" for name, tool in tool_names.items()
    )
    if options.api_key:
        env_list += "   - `AGENT_API_KEYS`: the keys callers send\n"
    if options.services:
        env_list += "   - nothing more for where it keeps things: `.env.example` matches `compose.yaml`\n"
    flags = {
        options.interface,
        "wheels" if isinstance(runtime, Wheels) else "pypi",
        *(["data"] if options.keeps_data else []),
        *(["compose"] if options.services else []),
        *(["api-key"] if options.api_key else []),
        *(["a2a"] if options.a2a else []),
    }
    run_doc = (
        "    uv run main.py                 # the agent and its UI on http://127.0.0.1:8000\n"
        "    cd web && npm run dev          # the UI with hot reload on :5173, /api sent here\n"
        "\nBuild the UI first (npm run build in web/).\n"
        if options.ui
        else "    uv run main.py                 # the agent's API on http://127.0.0.1:8000/api\n"
    )
    replacements = {
        "@@NAME@@": doc.name or slug,
        "@@SLUG@@": slug,
        "@@AGENT_ID@@": doc.id,
        "@@AGENT_FILE@@": agent_file,
        "@@VERSION_LABEL@@": version_label,
        "@@SERVICES@@": services,
        "@@CHAIN@@": chain,
        "@@IMPORTS@@": "AgentServer, env" if uses_env else "AgentServer",
        "@@RUN_DOC@@": run_doc,
        "@@CHOICES@@": _choices(options),
        "@@DEPENDENCIES@@": "".join(f"  {dep},\n" for dep in dependencies).rstrip("\n"),
        "@@UV_SOURCES@@": sources,
        "@@NPM_DEPENDENCIES@@": _indented_json(dict(sorted(npm.items())), "  "),
        "@@ENV_LIST@@": env_list or "   - nothing: its models need no keys\n",
        "@@RUNTIME_FROM@@": " (forge-agent-runtime from vendor/)" if isinstance(runtime, Wheels) else "",
        "@@CURL_AUTH@@": " -H 'authorization: Bearer <key>'" if options.api_key else "",
        "@@PHONY@@": " ".join(
            [
                "install",
                *(["services"] if options.services else []),
                "api",
                *(["web", "build"] if options.ui else []),
                "run",
                "docker",
            ]
        ),
        "@@INSTALL_WHAT@@": "The Python and Node dependencies" if options.ui else "The Python dependencies",
        "@@DOCKER_WHAT@@": "the UI and the agent" if options.ui else "the agent's API",
        "@@DRAFT_NOTE@@": (
            "> **A draft.** This is the agent's draft as it was when it was generated: it may not build\n"
            "> yet. Publish it in Forge and generate it again for a version that doesn't change.\n\n"
            if draft
            else ""
        ),
        "@@NOTES@@": (
            "\n## What needs more than this project\n\n"
            + "".join(f"- {note}\n" for note in notes if "draft" not in note)
            if any("draft" not in note for note in notes)
            else ""
        ),
    }

    def filled(content: bytes) -> bytes:
        text = _conditional(content.decode("utf-8"), flags)
        for placeholder, value in replacements.items():
            text = text.replace(placeholder, value)
        return text.encode("utf-8")

    for path, content in _walk(_template().joinpath("template")):
        if path.startswith("web/") and not options.ui:
            continue
        parent, _, base = path.rpartition("/")
        base = base.removesuffix(TEMPLATE_SUFFIX)
        renamed = DOTFILES.get(base, base)
        files[f"{parent}/{renamed}" if parent else renamed] = filled(content)
    if options.ui:
        for path, content in _walk(vendor):
            if path != "manifest.json":
                files[f"web/{path}"] = content
        files["web/components.json"] = _components_json(manifest.get("components") or {}).encode()
        files["web/src/agent-state.ts"] = _agent_state(doc).encode()
    files[agent_file] = (json.dumps(doc.to_json(), indent=2, ensure_ascii=False) + "\n").encode()
    files["agent/chat_agent.schema.json"] = _package().joinpath("chat_agent.schema.json").read_bytes()
    files["model_provider.yaml"] = model_provider_yaml.encode()
    files[".env.example"] = env_example.encode()
    files[".github/CODEOWNERS"] = _code_owners(options).encode()
    compose = _compose(options, doc.name or slug)
    if compose is not None:
        files["compose.yaml"] = compose.encode()
    return StarterProject(slug, files, notes, needs)
