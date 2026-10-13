"""
Pi (pi.dev), the coding agent, as a Python object: one ``pi --mode rpc``
process per :class:`PiAgent`, spoken to over its JSON-RPC protocol (JSONL on
stdin and stdout: https://pi.dev/docs/latest/rpc).

    options = PiOptions(folder=repo, model=PiModel(resolved, credential), tools=("read", "grep"))
    async with PiAgent(options) as pi:
        result = await pi.prompt("Where is the login handled?")
    result.text, result.tool_calls, result.usage

Each agent is kept apart from the machine it runs on, and from the others:

- **Its own pi.** A private folder of its own (``PI_CODING_AGENT_DIR``, and
  ``HOME``) holds pi's ``models.json`` (the one model, :mod:`.models`), its
  settings and the model's credential, and is removed when the agent closes.
  pi never sees the user's ``~/.pi``: no saved logins, sessions, extensions
  or skills.
- **Nothing it isn't given.** Extensions, skills, prompt templates and themes
  are off, and so is project trust (``--no-approve``), so a repository's own
  ``.pi`` settings or extensions never load; ``AGENTS.md``/``CLAUDE.md`` only
  when :attr:`PiOptions.context_files` says so. Its session isn't saved.
- **Only its tools.** Pi's built-in tools (:data:`TOOLS`), the ones allowed;
  none is none at all. ``bash`` runs any command as this process's user:
  give it only where that's meant.
- **A clean environment.** pi gets ``PATH``, its own ``HOME`` and what
  :attr:`PiOptions.env` adds, never this process's variables (its secrets).
- **Credentials read when they're used.** pi reads the model's credential
  from a private file for each request (``!cat``), rewritten as it's renewed
  (an OAuth2 token), never from its environment or its command line.

A prompt is answered once pi has settled (``agent_settled``: no retry,
compaction or queued message left): its answer is the last assistant
message's text. A model's error that pi gave up on, an abort, a timeout or
pi exiting raises :class:`PiError`. Closing ends pi (stdin closed, then
SIGTERM and SIGKILL to its process group, so its commands go too).
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import itertools
import json
import logging
import os
import shlex
import shutil
import signal
import tempfile
from collections import deque
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Any, Literal, Self

from forge_common.model_provider import ResolvedModel

from forge_pi_agent.models import model_ref, models_json

log = logging.getLogger(__name__)

Tool = Literal["read", "grep", "find", "ls", "bash", "edit", "write"]
ThinkingLevel = Literal["off", "minimal", "low", "medium", "high", "xhigh"]

#: Pi's built-in tools, in the order the builder lists them: reading and
#: searching, then running commands and changing files.
TOOLS: tuple[Tool, ...] = ("read", "grep", "find", "ls", "bash", "edit", "write")
#: The ones that change nothing.
READ_ONLY_TOOLS: tuple[Tool, ...] = ("read", "grep", "find", "ls")
#: How long pi thinks, as its ``--thinking`` takes it.
THINKING_LEVELS: tuple[ThinkingLevel, ...] = ("off", "minimal", "low", "medium", "high", "xhigh")
#: Gets the model's credential: an API key, or an OAuth2 token renewed before it expires.
Credential = Callable[[], Awaitable[str]]
#: Hears each of pi's events as it comes (sync or async).
OnEvent = Callable[[dict[str, Any]], Awaitable[None] | None]

#: The longest record pi may write: pi 0.x sends whole message snapshots with
#: each streamed delta.
MAX_RECORD_BYTES = 64 * 1024 * 1024
#: The most of pi's stderr kept, by line, to say why it failed.
STDERR_LINES = 40
_READ_CHUNK = 64 * 1024


class PiError(Exception):
    """pi couldn't do what it was asked: the message says why."""


class PiTimeout(PiError):
    """A prompt took longer than it may; pi was told to stop."""


@dataclass(frozen=True)
class PiModel:
    """
    The model pi runs on.

    :ivar model: One of model_provider.yaml's models.
    :ivar credential: Its credential, fetched when the agent starts and every
        ``refresh_seconds`` after (``ProviderModels.credential`` keeps an
        OAuth2 token fresh, so asking again is cheap).
    """

    model: ResolvedModel
    credential: Credential
    refresh_seconds: float | None = 60.0


@dataclass(frozen=True)
class PiOptions:
    """
    How a :class:`PiAgent` runs.

    :ivar folder: Where it works: pi's working directory, which must be there.
    :ivar model: What it runs on.
    :ivar tools: The built-in tools it may use (:data:`TOOLS`); none for none.
    :ivar thinking: How long it thinks; None for pi's default.
    :ivar instruction: Added to pi's own system prompt.
    :ivar context_files: Whether pi reads ``AGENTS.md`` and ``CLAUDE.md`` in
        its folder and the folders above it.
    :ivar command: How pi is started: ``("pi",)``, or e.g. ``("npx", "pi")``.
    :ivar env: Variables added to pi's environment (and its commands').
    :ivar start_timeout: Seconds pi has to start and answer.
    """

    folder: Path
    model: PiModel
    tools: Sequence[Tool] = READ_ONLY_TOOLS
    thinking: ThinkingLevel | None = None
    instruction: str = ""
    context_files: bool = False
    command: Sequence[str] = ("pi",)
    env: Mapping[str, str] = field(default_factory=dict)
    start_timeout: float = 30.0


@dataclass(frozen=True)
class PiToolCall:
    """One of pi's tool calls."""

    id: str
    tool: str
    args: dict[str, Any]
    is_error: bool = False
    #: Whether it finished.
    done: bool = False


@dataclass(frozen=True)
class PiUsage:
    """Tokens and cost of model calls, as pi counts them (``cost`` in USD, at models.json's prices)."""

    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    reasoning: int = 0
    cost: float = 0.0

    def __add__(self, other: PiUsage) -> PiUsage:
        return PiUsage(
            input=self.input + other.input,
            output=self.output + other.output,
            cache_read=self.cache_read + other.cache_read,
            cache_write=self.cache_write + other.cache_write,
            reasoning=self.reasoning + other.reasoning,
            cost=self.cost + other.cost,
        )

    @classmethod
    def of(cls, usage: Any) -> PiUsage:
        """:return: An assistant message's ``usage``."""
        if not isinstance(usage, dict):
            return cls()
        cost = usage.get("cost")
        return cls(
            input=_int(usage.get("input")),
            output=_int(usage.get("output")),
            cache_read=_int(usage.get("cacheRead")),
            cache_write=_int(usage.get("cacheWrite")),
            reasoning=_int(usage.get("reasoning")),
            cost=float(cost.get("total") or 0) if isinstance(cost, dict) else 0.0,
        )


@dataclass(frozen=True)
class PiModelCall:
    """One of pi's model calls: an assistant message."""

    provider: str
    model: str
    usage: PiUsage
    stop_reason: str
    error: str | None = None


@dataclass(frozen=True)
class PiResult:
    """What pi did for a prompt."""

    #: The last assistant message's text: pi's answer.
    text: str
    #: Why it stopped: ``stop``, ``length``…; ``handled`` when pi handled the
    #: prompt itself and no model ran.
    stop_reason: str
    tool_calls: list[PiToolCall]
    model_calls: list[PiModelCall]
    #: Every message the prompt added: pi's ``AgentMessage`` objects.
    messages: list[dict[str, Any]]

    @property
    def usage(self) -> PiUsage:
        """:return: Every model call's, added up."""
        total = PiUsage()
        for call in self.model_calls:
            total += call.usage
        return total


class PiAgent:
    """
    One pi process, in RPC mode: started on entering (or :meth:`start`), ended
    on leaving (or :meth:`close`). One prompt at a time.

    :param options: How it runs.
    """

    def __init__(self, options: PiOptions) -> None:
        self.options = options
        self._process: asyncio.subprocess.Process | None = None
        self._home: Path | None = None
        self._credential_file: Path | None = None
        self._tasks: list[asyncio.Task[None]] = []
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._listeners: list[asyncio.Queue[dict[str, Any] | None]] = []
        self._ids = itertools.count(1)
        self._stderr: deque[str] = deque(maxlen=STDERR_LINES)
        self._exited: str | None = None
        self._prompting = asyncio.Lock()

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.close()

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    # ------------------------------------------------------------------ starting

    async def start(self) -> None:
        """
        Start pi, and wait until it answers.

        :raises PiError: When it can't be started (not installed, its folder
            isn't there, a credential that can't be had) or doesn't answer.
        """
        if self._process is not None:
            raise PiError("pi is already started")
        options = self.options
        if not options.folder.is_dir():
            raise PiError(f"pi's folder isn't there: {options.folder}")
        unknown = [tool for tool in options.tools if tool not in TOOLS]
        if unknown:
            raise PiError(f"pi has no tool {', '.join(unknown)}; its tools are {', '.join(TOOLS)}")
        home = self._home = Path(tempfile.mkdtemp(prefix="forge-pi-"))
        try:
            credential = await options.model.credential()
            self._write_credential(credential)
            self._write_config(home)
            argv = self._argv(home)
            try:
                self._process = await asyncio.create_subprocess_exec(
                    *argv,
                    cwd=options.folder,
                    env=self._env(home),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    # Its own process group: closing ends its commands too.
                    start_new_session=True,
                )
            except FileNotFoundError:
                raise PiError(
                    f"pi isn't installed here: {argv[0]} wasn't found "
                    "(npm install -g @earendil-works/pi-coding-agent)"
                ) from None
            self._tasks = [
                asyncio.create_task(self._read(), name="pi-stdout"),
                asyncio.create_task(self._read_stderr(), name="pi-stderr"),
            ]
            if options.model.refresh_seconds:
                self._tasks.append(asyncio.create_task(self._refresh(), name="pi-credential"))
            state = await self.request({"type": "get_state"}, timeout=options.start_timeout)
        except BaseException:
            await self.close()
            raise
        running = state.get("model") if isinstance(state, dict) else None
        if not isinstance(running, dict) or running.get("id") != options.model.model.model.id:
            await self.close()
            raise PiError(f"pi didn't start on {model_ref(options.model.model)}{self._said()}")

    def _argv(self, home: Path) -> list[str]:
        options = self.options
        argv = [
            *options.command,
            "--mode",
            "rpc",
            "--no-session",
            "--no-extensions",
            "--no-skills",
            "--no-prompt-templates",
            "--no-themes",
            "--no-approve",
            "--offline",
            "--model",
            model_ref(options.model.model),
        ]
        argv += ["--tools", ",".join(options.tools)] if options.tools else ["--no-tools"]
        if not options.context_files:
            argv.append("--no-context-files")
        if options.thinking:
            argv += ["--thinking", options.thinking]
        if options.instruction.strip():
            # From a file: pi reads a value that names a file as the file's text.
            appended = home / "instruction.md"
            appended.write_text(options.instruction, encoding="utf-8")
            argv += ["--append-system-prompt", str(appended)]
        return argv

    def _env(self, home: Path) -> dict[str, str]:
        (home / "home").mkdir(exist_ok=True)
        return {
            "PATH": os.environ.get("PATH", os.defpath),
            "HOME": str(home / "home"),
            "LANG": "C.UTF-8",
            "PI_CODING_AGENT_DIR": str(home),
            "PI_OFFLINE": "1",
            "PI_SKIP_VERSION_CHECK": "1",
            "PI_TELEMETRY": "0",
            **self.options.env,
        }

    def _write_config(self, home: Path) -> None:
        assert self._credential_file is not None
        config = models_json(
            self.options.model.model, api_key=f"!cat {shlex.quote(str(self._credential_file))}"
        )
        _private(home / "models.json", json.dumps(config, indent=2))
        # Never a repository's own settings or extensions, whatever a flag says.
        _private(home / "settings.json", json.dumps({"defaultProjectTrust": "never"}))

    def _write_credential(self, credential: str) -> None:
        assert self._home is not None
        self._credential_file = self._home / "credential"
        _private(self._credential_file, credential)

    async def _refresh(self) -> None:
        every = self.options.model.refresh_seconds or 60.0
        while True:
            await asyncio.sleep(every)
            try:
                self._write_credential(await self.options.model.credential())
            except Exception:
                log.warning("pi_agent: the model's credential couldn't be renewed", exc_info=True)

    # ------------------------------------------------------------------ talking

    async def request(self, command: Mapping[str, Any], *, timeout: float | None = 30.0) -> Any:
        """
        Send pi a command (``{"type": "get_state"}``…) and wait for its response.

        :return: The response's ``data`` (None for a command that has none).
        :raises PiError: When pi refuses it, exits, or doesn't answer in time.
        """
        if not self.running or self._process is None or self._process.stdin is None:
            raise PiError(f"pi isn't running{self._said()}")
        ident = f"forge-{next(self._ids)}"
        answered: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[ident] = answered
        try:
            self._process.stdin.write((json.dumps({**command, "id": ident}) + "\n").encode())
            await self._process.stdin.drain()
            async with asyncio.timeout(timeout):
                response = await answered
        except TimeoutError:
            raise PiError(
                f"pi didn't answer {command.get('type')} in {timeout:g} seconds{self._said()}"
            ) from None
        except (BrokenPipeError, ConnectionResetError):
            raise PiError(f"pi exited{self._said()}") from None
        finally:
            self._pending.pop(ident, None)
        if response.get("success") is not True:
            raise PiError(
                f"pi refused {command.get('type')}: {response.get('error') or 'no reason given'}"
            )
        return response.get("data")

    async def prompt(
        self, message: str, *, on_event: OnEvent | None = None, timeout: float | None = None
    ) -> PiResult:
        """
        Ask pi, and wait until it has settled.

        :param message: What it's asked.
        :param on_event: Hears each event as it comes (``message_update``,
            ``tool_execution_start``…).
        :param timeout: Seconds it may take; past them pi is told to stop
            (abort) and :class:`PiTimeout` raised.
        :return: What it answered and did.
        :raises PiError: When it fails, gives up on an error, or exits.
        """
        async with self._prompting:
            events: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
            self._listeners.append(events)
            run = _Run()
            try:
                async with asyncio.timeout(timeout):
                    accepted = await self.request({"type": "prompt", "message": message})
                    # pi 1.x: a prompt pi handles itself (a command) starts no run.
                    if isinstance(accepted, dict) and accepted.get("disposition") == "handled":
                        return run.result(handled=True)
                    while True:
                        event = await events.get()
                        if event is None:
                            raise PiError(f"pi exited while it worked{self._said()}")
                        run.see(event)
                        if on_event is not None:
                            heard = on_event(event)
                            if inspect.isawaitable(heard):
                                await heard
                        if event.get("type") == "agent_settled":
                            break
            except TimeoutError:
                with contextlib.suppress(PiError):
                    await self.request({"type": "abort"}, timeout=10)
                raise PiTimeout(
                    f"pi took longer than {timeout:g} seconds; it was stopped"
                ) from None
            finally:
                self._listeners.remove(events)
            return run.result()

    async def abort(self) -> None:
        """Tell pi to stop what it's doing; it answers once it has."""
        await self.request({"type": "abort"})

    # ------------------------------------------------------------------ reading

    async def _read(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        stdout = self._process.stdout
        buffer = bytearray()
        try:
            while chunk := await stdout.read(_READ_CHUNK):
                buffer += chunk
                # Records end at LF only: U+2028/U+2029 may be inside one.
                while (end := buffer.find(b"\n")) >= 0:
                    line = bytes(buffer[:end]).rstrip(b"\r")
                    del buffer[: end + 1]
                    if line.strip():
                        self._dispatch(line)
                if len(buffer) > MAX_RECORD_BYTES:
                    self._exited = f"a record longer than {MAX_RECORD_BYTES} bytes"
                    break
        finally:
            self._ended()

    def _dispatch(self, line: bytes) -> None:
        try:
            record = json.loads(line)
        except ValueError:
            log.warning("pi_agent: pi wrote a line that isn't JSON: %.200r", line)
            return
        if not isinstance(record, dict):
            return
        if record.get("type") == "response":
            waiting = self._pending.get(str(record.get("id")))
            if waiting is not None and not waiting.done():
                waiting.set_result(record)
            return
        for listener in self._listeners:
            listener.put_nowait(record)

    def _ended(self) -> None:
        """pi's stdout closed: nothing more will be answered."""
        for waiting in self._pending.values():
            if not waiting.done():
                waiting.set_result({"success": False, "error": f"pi exited{self._said()}"})
        for listener in self._listeners:
            listener.put_nowait(None)

    async def _read_stderr(self) -> None:
        assert self._process is not None and self._process.stderr is not None
        async for raw in self._process.stderr:
            line = raw.decode(errors="replace").rstrip()
            if line:
                self._stderr.append(line)
                log.debug("pi_agent: pi: %s", line)

    def _said(self) -> str:
        """:return: What pi last wrote to stderr, to say why it failed; nothing when it wrote nothing."""
        said = " | ".join(list(self._stderr)[-5:])
        reason = self._exited or said
        return f": {reason[:1000]}" if reason else ""

    # ------------------------------------------------------------------ closing

    async def close(self, *, grace: float = 5.0) -> None:
        """End pi (and its commands), and remove its private folder."""
        process = self._process
        try:
            if process is not None and process.returncode is None:
                if process.stdin is not None:
                    with contextlib.suppress(Exception):
                        process.stdin.close()
                try:
                    async with asyncio.timeout(grace):
                        await process.wait()
                except TimeoutError:
                    _signal(process, signal.SIGTERM)
                    try:
                        async with asyncio.timeout(grace):
                            await process.wait()
                    except TimeoutError:
                        _signal(process, signal.SIGKILL)
                        await process.wait()
            if process is not None:
                # Commands it started that outlive it.
                _signal(process, signal.SIGKILL)
        finally:
            for task in self._tasks:
                task.cancel()
            for task in self._tasks:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            self._tasks = []
            if self._home is not None:
                shutil.rmtree(self._home, ignore_errors=True)
                self._home = None


class _Run:
    """What one prompt's events add up to."""

    def __init__(self) -> None:
        self.tools: dict[str, PiToolCall] = {}
        self.calls: list[PiModelCall] = []
        self.messages: list[dict[str, Any]] = []
        self.last: dict[str, Any] | None = None
        self.retry_error: str | None = None

    def see(self, event: dict[str, Any]) -> None:
        kind = event.get("type")
        if kind == "tool_execution_start":
            ident = str(event.get("toolCallId"))
            args = event.get("args")
            self.tools[ident] = PiToolCall(
                id=ident,
                tool=str(event.get("toolName")),
                args=args if isinstance(args, dict) else {},
            )
        elif kind == "tool_execution_end":
            ident = str(event.get("toolCallId"))
            started = self.tools.get(ident) or PiToolCall(
                id=ident, tool=str(event.get("toolName")), args={}
            )
            self.tools[ident] = PiToolCall(
                id=ident,
                tool=started.tool,
                args=started.args,
                is_error=event.get("isError") is True,
                done=True,
            )
        elif kind == "message_end":
            message = event.get("message")
            if not isinstance(message, dict):
                return
            self.messages.append(message)
            if message.get("role") == "assistant":
                self.last = message
                self.calls.append(
                    PiModelCall(
                        provider=str(message.get("provider") or ""),
                        model=str(message.get("model") or ""),
                        usage=PiUsage.of(message.get("usage")),
                        stop_reason=str(message.get("stopReason") or ""),
                        error=message.get("errorMessage")
                        if isinstance(message.get("errorMessage"), str)
                        else None,
                    )
                )
        elif kind == "auto_retry_end" and event.get("success") is False:
            final = event.get("finalError")
            self.retry_error = str(final) if final else None

    def result(self, *, handled: bool = False) -> PiResult:
        if handled:
            return PiResult(
                text="", stop_reason="handled", tool_calls=[], model_calls=[], messages=[]
            )
        last = self.last
        if last is None:
            raise PiError(
                f"pi answered nothing{': ' + self.retry_error if self.retry_error else ''}"
            )
        reason = str(last.get("stopReason") or "")
        if reason in ("error", "aborted"):
            why = last.get("errorMessage") or self.retry_error or reason
            raise PiError(f"pi's model failed: {why}" if reason == "error" else "pi was stopped")
        return PiResult(
            text=text_of(last),
            stop_reason=reason,
            tool_calls=list(self.tools.values()),
            model_calls=self.calls,
            messages=self.messages,
        )


def text_of(message: Mapping[str, Any]) -> str:
    """:return: An assistant message's text blocks, one after another."""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "".join(
        block["text"]
        for block in content
        if isinstance(block, dict)
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
    ).strip()


def json_answer(text: str) -> Any:
    """
    :return: An answer that's JSON, parsed: the whole text, or a fenced
        `````json`` block in it.
    :raises ValueError: When it isn't JSON.
    """
    stripped = text.strip()
    if stripped.startswith("```"):
        _, _, rest = stripped.partition("\n")
        body, fence, _ = rest.rpartition("```")
        if fence:
            stripped = body.strip()
    try:
        return json.loads(stripped)
    except ValueError:
        start = min((i for i in (stripped.find("{"), stripped.find("[")) if i >= 0), default=-1)
        end = max(stripped.rfind("}"), stripped.rfind("]"))
        if start < 0 or end <= start:
            raise ValueError("the answer isn't JSON") from None
        return json.loads(stripped[start : end + 1])


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _private(path: Path, text: str) -> None:
    """Write a file only this process's user reads, all at once."""
    partial = path.with_name(path.name + ".partial")
    descriptor = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as file:
        file.write(text)
    os.replace(partial, path)


def _signal(process: asyncio.subprocess.Process, sent: signal.Signals) -> None:
    """Signal pi's process group: pi and the commands it started."""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, sent)
