"""
The side effects the suite counts: a local HTTP server that records every
request it gets, by name, the moment it arrives (that is when an effect has
happened, whatever becomes of its answer), and can hold the next requests to
a name until the test releases them, so a test knows a step is in flight.

- ``/effect/<name>`` (any method): an effect. HTTP steps call it; Bash steps
  call it with curl.
- ``/v1/chat/completions``: an OpenAI-compatible model (the worker's model
  provider configuration points at it), recorded as the effect ``llm``. It
  calls the first tool it's offered until a tool has answered, then answers
  in words, quoting the tool's answer; with no tools, it answers ``ok``.

It runs on threads of its own in the test process, so a held request never
holds the test's event loop.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

#: The longest a held request waits for its release before it's answered anyway.
HOLD_LIMIT = 600.0
LLM = "llm"


@dataclass
class Hit:
    """One request: when it arrived (``time.time()``), and what it was."""

    name: str
    at: float
    method: str
    path: str
    body: Any
    #: Whether it was held, and when it was answered (None: not yet, or the client went first).
    held: bool = False
    answered_at: float | None = None


@dataclass
class _Hold:
    """Holds the next ``left`` requests to a name, each until it's released."""

    left: int
    waiting: list[threading.Event] = field(default_factory=list)


class MockServer:
    """The server, on 127.0.0.1 and a port of its own (``url``)."""

    def __init__(self) -> None:
        self._lock = threading.Condition()
        self.hits: list[Hit] = []
        self._holds: dict[str, list[_Hold]] = defaultdict(list)
        server = self
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(server))
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, name="mock-server", daemon=True)

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> MockServer:
        self._thread.start()
        return self

    def close(self) -> None:
        self.release_all()
        self._server.shutdown()
        self._server.server_close()

    def reset(self) -> None:
        """Forget every request and hold (each test starts afresh)."""
        self.release_all()
        with self._lock:
            self.hits.clear()
            self._holds.clear()

    # ------------------------------------------------------------------ the tests' side

    def effect_url(self, name: str) -> str:
        return f"{self.url}/effect/{name}"

    def count(self, name: str) -> int:
        with self._lock:
            return sum(1 for hit in self.hits if hit.name == name)

    def of(self, name: str) -> list[Hit]:
        with self._lock:
            return [hit for hit in self.hits if hit.name == name]

    def hold(self, name: str, times: int = 1) -> None:
        """The next ``times`` requests to ``name`` wait until :meth:`release`."""
        with self._lock:
            self._holds[name].append(_Hold(left=times))

    def release(self, name: str, count: int | None = None) -> None:
        """Answer the requests to ``name`` held so far, oldest first: ``count``
        of them; or (None) all of them, and hold no more."""
        with self._lock:
            if count is None:
                events = [event for hold in self._holds.pop(name, []) for event in hold.waiting]
            else:
                events = []
                for hold in self._holds.get(name, []):
                    while hold.waiting and len(events) < count:
                        events.append(hold.waiting.pop(0))
        for event in events:
            event.set()

    def release_all(self) -> None:
        with self._lock:
            events = [event for each in self._holds.values() for hold in each for event in hold.waiting]
            self._holds.clear()
        for event in events:
            event.set()

    async def arrived(self, name: str, count: int = 1, *, seconds: float = 60) -> list[Hit]:
        """:return: The requests to ``name``, once ``count`` of them have arrived."""
        deadline = time.monotonic() + seconds
        while self.count(name) < count:
            if time.monotonic() > deadline:
                raise AssertionError(f"{count} request(s) to {name} didn't arrive in {seconds:g}s: {self.count(name)}")
            await asyncio.sleep(0.05)
        return self.of(name)

    # ------------------------------------------------------------------ the server's side

    def _arrive(self, name: str, method: str, path: str, body: Any) -> tuple[Hit, threading.Event | None]:
        """:return: The request recorded, and what it waits for when it's held."""
        with self._lock:
            hit = Hit(name=name, at=time.time(), method=method, path=path, body=body)
            self.hits.append(hit)
            holds = self._holds.get(name) or []
            hold = next((each for each in holds if each.left > 0), None)
            released: threading.Event | None = None
            if hold is not None:
                hold.left -= 1
                hit.held = True
                released = threading.Event()
                hold.waiting.append(released)
            self._lock.notify_all()
        return hit, released


def _handler(server: MockServer) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - BaseHTTPRequestHandler's
            return

        def _body(self) -> Any:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                return json.loads(raw) if raw else None
            except ValueError:
                return raw.decode(errors="replace")

        def _answer(self, status: int, payload: Any) -> None:
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _serve(self) -> None:
            path = self.path.partition("?")[0]
            body = self._body()
            if path.startswith("/effect/"):
                name = path.removeprefix("/effect/").strip("/") or "effect"
            elif path.rstrip("/").endswith("/chat/completions"):
                name = LLM
            else:
                self._answer(404, {"error": f"no {path}"})
                return
            hit, released = server._arrive(name, self.command, self.path, body)
            if released is not None:
                released.wait(HOLD_LIMIT)
            hit.answered_at = time.time()
            if name == LLM:
                self._answer(200, _completion(body if isinstance(body, dict) else {}))
            else:
                self._answer(200, {"ok": True, "name": name, "count": server.count(name)})

        do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _serve

    return Handler


def _completion(request: dict[str, Any]) -> dict[str, Any]:
    """A chat completion: the first tool offered, called, until a tool answered; then words."""
    messages = request.get("messages") or []
    tools = request.get("tools") or []
    last = messages[-1] if messages else {}
    message: dict[str, Any]
    if last.get("role") == "tool":
        answered = last.get("content")
        if not isinstance(answered, str):
            answered = json.dumps(answered)
        message = {"role": "assistant", "content": f"done: {answered[:1500]}"}
        finish = "stop"
    elif tools:
        name = tools[0].get("function", {}).get("name", "tool")
        call = {
            "id": f"call_{int(time.time() * 1000)}",
            "type": "function",
            "function": {"name": name, "arguments": "{}"},
        }
        message = {"role": "assistant", "content": None, "tool_calls": [call]}
        finish = "tool_calls"
    else:
        message = {"role": "assistant", "content": "ok"}
        finish = "stop"
    return {
        "id": f"chatcmpl-{int(time.time() * 1000)}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": request.get("model") or "mock-1",
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }
