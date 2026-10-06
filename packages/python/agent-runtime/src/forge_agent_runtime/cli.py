"""``forge-agent``: check an exported chat agent, or serve it on its own.

::

    forge-agent validate support-assistant.chat-agent.json
    forge-agent serve support-assistant.chat-agent.json --port 8000
    forge-agent serve ./agents/            # every agent in a folder
    forge-agent new support-assistant.chat-agent.json   # a project of its own (starter)

``serve`` needs ``forge-agent-runtime[server]``. Settings come from flags or
``FORGE_AGENT_*`` environment variables (and a ``.env`` file):

- ``FORGE_AGENT_MODEL_PROVIDER_CONFIG``: the model_provider.yaml the agents
  run on (its keys as ``${NAME}``); forge-common's shared one by default.
- ``FORGE_AGENT_SESSIONS``: ``memory`` (the default) or a database URL
  (``sqlite+aiosqlite:///sessions.db``, ``mysql+aiomysql://…``) to keep
  conversations in.
- ``FORGE_AGENT_MEMORY_ATLAS_URI`` / ``_MEMORY_DATABASE``: long-term memory
  in MongoDB Atlas, embedded by the model provider config's OpenAI provider
  (``forge-agent-runtime[memory-atlas]``); in memory otherwise.
- ``FORGE_AGENT_ALLOW_PRIVATE`` / ``_ALLOWED_HOSTS``: what HTTP tools may reach.
- ``FORGE_AGENT_API_PREFIX`` (``--prefix``): where the run API is mounted; at
  the root by default here, as ``adk api_server`` serves it.
- ``FORGE_AGENT_A2A`` (``--a2a``): Google's A2A protocol too, at ``/a2a``
  (``forge-agent-runtime[a2a]``).

Every other setting is :class:`~forge_agent_runtime.app.AgentServerSettings`'s.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Any

from forge_agent_runtime.app import AgentServer, AgentServerSettings
from forge_agent_runtime.document import DocumentError, load_document

#: The settings ``serve`` reads; the builder's (``FORGE_AGENT_*``).
ServeSettings = AgentServerSettings


def validate(path: Path) -> int:
    targets = sorted(path.glob("*.json")) if path.is_dir() else [path]
    failed = 0
    for target in targets:
        try:
            doc = load_document(target)
        except (DocumentError, OSError) as exc:
            failed += 1
            print(f"✗ {target}")
            for problem in getattr(exc, "problems", [str(exc)]):
                print(f"    {problem}")
            continue
        version = f"@{doc.version}" if doc.version is not None else ""
        print(f"✓ {target}: {doc.name} ({doc.id}{version}), {len(doc.nodes)} nodes")
    return 1 if failed else 0


def build_executor(path: Path, settings: AgentServerSettings) -> Any:
    """:return: An executor for the agent (or folder of agents) at ``path``, as ``settings`` say."""
    return AgentServer(settings).with_agent(path).executor


def serve(path: Path, settings: AgentServerSettings) -> int:
    try:
        import uvicorn  # noqa: F401
    except ImportError:
        print(
            "forge-agent serve needs the server extra: pip install 'forge-agent-runtime[server]'",
            file=sys.stderr,
        )
        return 2
    if validate(path):
        return 1
    if path.is_file():
        doc = load_document(path)

        # Built once now, so a missing setting or tool shows before the server starts.
        async def check() -> None:
            trial = build_executor(path, settings)
            try:
                await trial.runner(doc.id)
            finally:
                await trial.close()

        try:
            asyncio.run(check())
        except Exception as exc:
            print(f"✗ {doc.name} can't be built: {exc}", file=sys.stderr)
            return 1
        print(
            f"Serving {doc.name} as appName {doc.id!r} at "
            f"http://{settings.host}:{settings.port}{settings.api_prefix}"
        )
        if settings.a2a:
            print(f"and over A2A at http://{settings.host}:{settings.port}/a2a")
    AgentServer(settings.model_copy(update={"prebuild": False})).with_agent(path).run()
    return 0


def new(args: argparse.Namespace) -> int:
    """Writes a standalone project for an exported agent (``forge_agent_runtime.starter``)."""
    from pydantic import ValidationError

    from forge_agent_runtime.starter import StarterOptions, Wheels, generate_project, project_name

    try:
        doc = load_document(args.path)
    except (DocumentError, OSError) as exc:
        print(f"✗ {args.path}: {exc}", file=sys.stderr)
        return 1
    try:
        options = StarterOptions(
            interface="api" if args.api_only else "ui",
            sessions=args.sessions,
            artifacts=args.artifacts,
            memory=args.memory,
            streaming=not args.no_streaming,
            a2a=not args.no_a2a,
            api_key=args.api_key,
            cors_origins=args.cors_origin or [],
            code_owners=args.code_owner or [],
        )
    except ValidationError as exc:
        for error in exc.errors():
            print(f"✗ {error['msg'].removeprefix('Value error, ')}", file=sys.stderr)
        return 1
    out: Path = args.out or Path(project_name(args.name or doc.name))
    # What it writes: the folder, or the zip beside it.
    taken = out.with_suffix(".zip").exists() if args.zip else out.exists() and any(out.iterdir())
    if taken and not args.force:
        print(f"✗ {out} is taken: pick another --out, or --force to write over it.", file=sys.stderr)
        return 1
    try:
        project = generate_project(
            doc,
            name=args.name,
            model_provider_yaml=args.model_provider.read_text("utf-8") if args.model_provider else None,
            runtime=Wheels(args.wheels) if args.wheels else None,
            options=options,
        )
    except FileNotFoundError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1
    if args.zip:
        target = out.with_suffix(".zip")
        target.write_bytes(project.zip())
    else:
        target = project.write(out)
    print(f"✓ {doc.name} → {target} ({len(project.files)} files)")
    for note in project.notes:
        print(f"  ! {note}")
    print(f"  Next: cd {out}, cp .env.example .env and fill it in, then see README.md.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="forge-agent", description="Check or serve Forge chat agents.")
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("validate", help="Check an exported agent (or a folder of them).")
    check.add_argument("path", type=Path)
    run = commands.add_parser("serve", help="Serve an agent (or a folder of them) over ADK's run API.")
    run.add_argument("path", type=Path)
    run.add_argument("--host")
    run.add_argument("--port", type=int)
    run.add_argument("--prefix", dest="api_prefix")
    run.add_argument("--model-provider", dest="model_provider_config", type=Path)
    run.add_argument("--sessions", help="memory, or a database URL")
    run.add_argument("--memory-atlas-uri")
    run.add_argument("--allow-private", action="store_true", default=None)
    run.add_argument(
        "--a2a", action="store_true", default=None, help="Serve over Google's A2A protocol too (/a2a)"
    )
    make = commands.add_parser("new", help="Generate a standalone project (server and UI) for an agent.")
    make.add_argument("path", type=Path)
    make.add_argument("--out", type=Path, help="Where to write it; the agent's name by default")
    make.add_argument("--name", help="The project's name; the agent's by default")
    make.add_argument("--model-provider", type=Path, help="The model_provider.yaml it runs on")
    make.add_argument("--wheels", type=Path, help="Bundle the runtime's wheels from this folder (uv build)")
    make.add_argument("--zip", action="store_true", help="Write a zip instead of a folder")
    make.add_argument("--force", action="store_true", help="Write into a folder that isn't empty")
    make.add_argument(
        "--sessions",
        choices=["memory", "sqlite", "postgresql", "mysql"],
        default="memory",
        help="Where conversations are kept",
    )
    make.add_argument(
        "--artifacts",
        choices=["memory", "folder", "s3"],
        default="memory",
        help="Where files the agent's tools save are kept",
    )
    make.add_argument(
        "--memory", choices=["memory", "atlas"], default="memory", help="Where long-term memory is kept"
    )
    make.add_argument("--no-streaming", action="store_true", help="Replies arrive whole, not streamed")
    make.add_argument("--api-only", action="store_true", help="The API alone, without the chat UI")
    make.add_argument("--no-a2a", action="store_true", help="Without Google's A2A protocol (/a2a)")
    make.add_argument("--api-key", action="store_true", help="The API asks for a key (with --api-only)")
    make.add_argument(
        "--cors-origin", action="append", metavar="ORIGIN", help="A page elsewhere that may call the API"
    )
    make.add_argument(
        "--code-owner", action="append", metavar="OWNER", help="Who reviews changes: @user, @org/team"
    )
    args = parser.parse_args(argv)
    if args.command == "validate":
        return validate(args.path)
    if args.command == "new":
        return new(args)
    overrides = {
        key: value
        for key, value in vars(args).items()
        if key not in ("command", "path") and value is not None
    }
    # Served at the root, as adk api_server serves it, unless told otherwise.
    if "api_prefix" not in overrides and "FORGE_AGENT_API_PREFIX" not in os.environ:
        overrides["api_prefix"] = ""
    settings = AgentServerSettings(**overrides)
    return serve(args.path, settings)


if __name__ == "__main__":
    sys.exit(main())
