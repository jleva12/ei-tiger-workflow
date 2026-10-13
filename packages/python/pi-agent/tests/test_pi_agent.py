"""
PiAgent against a stand-in pi (forge_pi_agent.fake_pi) that speaks pi's RPC protocol, and
the models.json it's given; FORGE_TEST_PI=1 also runs one prompt on the real
pi (on PATH) with OPENAI_API_KEY.
"""

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest
from forge_common.model_provider import ResolvedModel, parse_model_provider_yaml

from forge_pi_agent import (
    PiAgent,
    PiError,
    PiModel,
    PiOptions,
    PiTimeout,
    json_answer,
    model_ref,
    models_json,
)
from forge_pi_agent.fake_pi import COMMAND

FAKE_PI = COMMAND

CONFIG = """
version: 1
default: {provider: openai, model: gpt-5.2}
providers:
  openai:
    name: OpenAI
    baseUrl: https://api.openai.com/v1
    api: openai-responses
    authHeader: true
    headers: {X-Tenant: "acme$1", X-Odd: "!rm -rf /"}
    auth: {type: apiKey, key: "${OPENAI_API_KEY}"}
    models:
      - id: gpt-5.2
        name: GPT-5.2
        reasoning: true
        thinkingLevelMap: {off: none, minimal: null, xhigh: xhigh}
        input: [text, image]
        contextWindow: 400000
        maxTokens: 128000
        cost: {input: 1.75, output: 14, cacheRead: 0.175}
        compat: {supportsStrictMode: true}
  local:
    baseUrl: http://localhost:11434/v1
    api: openai-completions
    auth: {type: apiKey, key: ollama}
    models:
      - id: qwen
"""


def resolved(ref: str = "openai/gpt-5.2") -> ResolvedModel:
    found = parse_model_provider_yaml(CONFIG, {"OPENAI_API_KEY": "sk-test"}).find(ref)
    assert found is not None
    return found


def model(ref: str = "openai/gpt-5.2") -> PiModel:
    async def credential() -> str:
        return "sk-test"

    return PiModel(resolved(ref), credential)


def options(folder: Path, record: Path | None = None, **more: Any) -> PiOptions:
    env = {"FAKE_PI_RECORD": str(record)} if record else {}
    return PiOptions(
        folder=folder,
        model=more.pop("model", None) or model(),
        command=more.pop("command", FAKE_PI),
        env={**env, **more.pop("env", {})},
        **more,
    )


# ------------------------------------------------------------------ models.json


def test_a_model_is_pi_s_models_json_with_its_provider_alone() -> None:
    config = models_json(resolved(), api_key="!cat /x")

    assert config == {
        "providers": {
            "forge-openai": {
                "baseUrl": "https://api.openai.com/v1",
                "api": "openai-responses",
                "authHeader": True,
                "apiKey": "!cat /x",
                # Already resolved: pi reads them back as they are, never as $NAME or a command.
                "headers": {"X-Tenant": "acme$$1", "X-Odd": "$!rm -rf /"},
                "models": [
                    {
                        "id": "gpt-5.2",
                        "name": "GPT-5.2",
                        "reasoning": True,
                        "input": ["text", "image"],
                        "contextWindow": 400000,
                        "maxTokens": 128000,
                        "cost": {
                            "input": 1.75,
                            "output": 14.0,
                            "cacheRead": 0.175,
                            "cacheWrite": 0.0,
                        },
                        # A level left out isn't one set to null.
                        "thinkingLevelMap": {"off": "none", "minimal": None, "xhigh": "xhigh"},
                        "compat": {"supportsStrictMode": True},
                    }
                ],
            }
        }
    }
    assert model_ref(resolved()) == "forge-openai/gpt-5.2"


def test_a_plain_model_takes_pi_s_defaults() -> None:
    [provider] = models_json(resolved("local/qwen"), api_key="k")["providers"].values()

    assert "headers" not in provider
    assert provider["models"] == [
        {
            "id": "qwen",
            "name": "qwen",
            "reasoning": False,
            "input": ["text"],
            "contextWindow": 128000,
            "maxTokens": 16384,
            "cost": {"input": 0.0, "output": 0.0, "cacheRead": 0.0, "cacheWrite": 0.0},
        }
    ]


@pytest.mark.parametrize(
    ("text", "parsed"),
    [
        ('{"a": 1}', {"a": 1}),
        ('```json\n{"a": [1, 2]}\n```', {"a": [1, 2]}),
        ("Here it is:\n```\n[1]\n```\nDone.", [1]),
        ('The answer: {"ok": true} as asked', {"ok": True}),
    ],
)
def test_an_answer_that_s_json_is_parsed(text: str, parsed: Any) -> None:
    assert json_answer(text) == parsed


def test_an_answer_that_isnt_json_says_so() -> None:
    with pytest.raises(ValueError, match="isn't JSON"):
        json_answer("Nothing here.")


# ------------------------------------------------------------------ the process


async def test_pi_runs_in_its_folder_with_its_tools_on_its_model_and_nothing_else(
    tmp_path: Path,
) -> None:
    (tmp_path / "work").mkdir()
    (tmp_path / "work" / "app.py").write_text("print(1)")
    record = tmp_path / "record.json"
    os.environ["FORGE_PI_TEST_SECRET"] = "worker-secret"
    try:
        async with PiAgent(
            options(
                tmp_path / "work",
                record,
                tools=("read", "ls"),
                thinking="low",
                instruction="Only ever read.",
                env={"FAKE_PI_RECORD": str(record), "GIT_TERMINAL_PROMPT": "0"},
            )
        ) as pi:
            home = Path(json.loads(record.read_text())["env"]["PI_CODING_AGENT_DIR"])
            assert home.is_dir()
            result = await pi.prompt("hello")
    finally:
        del os.environ["FORGE_PI_TEST_SECRET"]

    started = json.loads(record.read_text())
    args = started["args"]
    assert args[: args.index("--model")] == [
        "--mode",
        "rpc",
        "--no-session",
        "--no-extensions",
        "--no-skills",
        "--no-prompt-templates",
        "--no-themes",
        "--no-approve",
        "--offline",
    ]
    assert args[args.index("--model") + 1] == "forge-openai/gpt-5.2"
    assert args[args.index("--tools") + 1] == "read,ls"
    assert args[args.index("--thinking") + 1] == "low"
    assert "--no-context-files" in args
    appended = Path(args[args.index("--append-system-prompt") + 1])
    assert appended.parent == home
    assert started["cwd"] == str((tmp_path / "work").resolve())
    assert started["credential"] == "sk-test"
    assert started["settings"] == {"defaultProjectTrust": "never"}
    assert started["models"]["providers"]["forge-openai"]["apiKey"].startswith("!cat ")
    # Its own environment: never the worker's.
    assert "FORGE_PI_TEST_SECRET" not in started["env"]
    assert "sk-test" not in json.dumps(started["env"])
    assert started["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert started["env"]["HOME"] == str(home / "home")
    # Its private folder goes when it closes.
    assert not home.exists()

    assert result.text == "you said: hello\u2028."
    assert result.stop_reason == "stop"
    assert [(call.tool, call.args, call.is_error, call.done) for call in result.tool_calls] == [
        ("ls", {"path": "."}, False, True)
    ]
    assert [call.stop_reason for call in result.model_calls] == ["toolUse", "stop"]
    assert result.usage.input == 16
    assert result.usage.output == 10
    assert result.usage.cache_read == 4
    assert result.usage.cost == pytest.approx(0.02)


async def test_no_tools_is_none_at_all_and_context_files_are_read_when_asked(
    tmp_path: Path,
) -> None:
    record = tmp_path / "record.json"
    async with PiAgent(options(tmp_path, record, tools=(), context_files=True)) as pi:
        result = await pi.prompt("hello")

    args = json.loads(record.read_text())["args"]
    assert "--no-tools" in args
    assert "--tools" not in args
    assert "--no-context-files" not in args
    assert "--thinking" not in args
    assert "--append-system-prompt" not in args
    assert result.tool_calls == []


async def test_a_renewed_credential_is_the_one_pi_reads(tmp_path: Path) -> None:
    record = tmp_path / "record.json"
    renewed = asyncio.Event()

    async def credential() -> str:
        return "token-2" if renewed.is_set() else "token-1"

    every_so_often = PiModel(resolved(), credential, refresh_seconds=0.05)
    async with PiAgent(options(tmp_path, record, model=every_so_often)) as pi:
        home = Path(json.loads(record.read_text())["env"]["PI_CODING_AGENT_DIR"])
        # pi read the first when it started…
        assert json.loads(record.read_text())["credential"] == "token-1"
        assert (home / "credential").stat().st_mode & 0o777 == 0o600
        renewed.set()
        # …and reads the renewed one from then on.
        for _ in range(100):
            if (home / "credential").read_text() == "token-2":
                break
            await asyncio.sleep(0.02)
        assert (home / "credential").read_text() == "token-2"
        assert pi.running


async def test_events_are_heard_as_they_come_and_wide_records_are_read_whole(
    tmp_path: Path,
) -> None:
    heard: list[str] = []

    async def hear(event: dict[str, Any]) -> None:
        heard.append(event["type"])

    async with PiAgent(options(tmp_path)) as pi:
        result = await pi.prompt("wide", on_event=hear)
        # A listener that isn't async is heard too.
        again = await pi.prompt("hello", on_event=lambda event: heard.append(event["type"]))

    assert result.text == "wide"
    assert heard[0] == "agent_start"
    assert "message_update" in heard
    assert heard.count("agent_settled") == 2
    assert again.text.startswith("you said: hello")


async def test_a_model_that_fails_fails_the_prompt(tmp_path: Path) -> None:
    async with PiAgent(options(tmp_path)) as pi:
        with pytest.raises(PiError, match="pi's model failed: 429 rate limited"):
            await pi.prompt("fail")
        # pi is still there for the next.
        assert (await pi.prompt("hello")).text.startswith("you said")


async def test_a_prompt_that_takes_too_long_is_stopped(tmp_path: Path) -> None:
    async with PiAgent(options(tmp_path)) as pi:
        with pytest.raises(PiTimeout, match=r"longer than 0\.3 seconds"):
            await pi.prompt("slow", timeout=0.3)


async def test_pi_exiting_fails_the_prompt_saying_what_it_said(tmp_path: Path) -> None:
    async with PiAgent(options(tmp_path)) as pi:
        with pytest.raises(PiError, match=r"pi exited.*told to exit"):
            await pi.prompt("exit")
        assert not pi.running


async def test_a_command_pi_refuses_says_why(tmp_path: Path) -> None:
    async with PiAgent(options(tmp_path)) as pi:
        with pytest.raises(PiError, match="pi refused get_tree: Unknown command: get_tree"):
            await pi.request({"type": "get_tree"})


async def test_pi_that_isnt_installed_says_so(tmp_path: Path) -> None:
    with pytest.raises(PiError, match="pi isn't installed here"):
        async with PiAgent(options(tmp_path, command=("no-such-pi-anywhere",))):
            pass


async def test_a_folder_that_isnt_there_or_a_tool_pi_hasnt_are_refused(tmp_path: Path) -> None:
    with pytest.raises(PiError, match="folder isn't there"):
        await PiAgent(options(tmp_path / "missing")).start()
    with pytest.raises(PiError, match="no tool web"):
        await PiAgent(options(tmp_path, tools=("read", "web"))).start()  # type: ignore[arg-type]


async def test_closing_ends_the_commands_pi_started(tmp_path: Path) -> None:
    script = tmp_path / "pi.py"
    # A pi that leaves a command running behind it.
    script.write_text(
        "import os, subprocess, sys\n"
        "subprocess.Popen(['sleep', '60'])\n"
        "os.execv(sys.executable, [sys.executable, '-m', 'forge_pi_agent.fake_pi', *sys.argv[1:]])\n"
    )
    pi = PiAgent(options(tmp_path, command=(sys.executable, str(script))))
    await pi.start()
    pgid = pi._process.pid  # type: ignore[union-attr]
    await pi.close()

    await asyncio.sleep(0.2)
    with pytest.raises(ProcessLookupError):
        os.killpg(pgid, 0)


# ------------------------------------------------------------------ the real pi


@pytest.mark.pi
@pytest.mark.skipif(
    os.environ.get("FORGE_TEST_PI") != "1", reason="FORGE_TEST_PI=1 runs the real pi"
)
async def test_the_real_pi_reads_a_folder(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("print('hello')\n# TODO: wire the login\n")
    key = os.environ["OPENAI_API_KEY"]

    async def credential() -> str:
        return key

    found = parse_model_provider_yaml(
        CONFIG.replace("gpt-5.2", "gpt-5.6-luna").replace("reasoning: true", "reasoning: true"),
        {"OPENAI_API_KEY": key},
    ).find("openai/gpt-5.6-luna")
    assert found is not None
    async with PiAgent(
        PiOptions(folder=tmp_path, model=PiModel(found, credential), thinking="low")
    ) as pi:
        result = await pi.prompt(
            "What is the TODO in app.py? Answer with its words only.", timeout=120
        )

    assert "login" in result.text.lower()
    assert {call.tool for call in result.tool_calls} & {"read", "grep"}
    assert result.usage.input > 0
