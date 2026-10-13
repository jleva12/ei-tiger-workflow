"""
A stand-in pi, for tests of code that runs pi: ``pi --mode rpc`` as the
protocol has it (JSONL on stdin and stdout), started as
``PiOptions(command=COMMAND)``. It answers prompts by what they say:

- ``exit``: exits at once, saying why on stderr.
- ``fail``: the model fails (an assistant message whose stopReason is error).
- ``slow``: works until it's told to stop (abort).
- ``json <JSON>``: answers with that JSON (up to a blank line), fenced.
- ``Your answer doesn't fit``…: answers with ``$FAKE_PI_FIX`` (``{}`` unset), fenced.
- ``wide``: streams a delta far longer than a line reader's 64 KiB, then answers.
- anything else: lists its folder (the ``ls`` tool, when it has it), then
  answers ``you said: <prompt>`` and a raw U+2028 (JSONL splits on LF only).

Each answer counts 10 input tokens, 2 of them cached, and 5 output tokens, at
$0.01. It writes what it was started with (its arguments, environment, folder,
the credential its models.json reads and every prompt) to ``$FAKE_PI_RECORD``.
"""

import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

#: How to start it: ``PiOptions(command=COMMAND)``.
COMMAND = (sys.executable, "-m", "forge_pi_agent.fake_pi")


class _FakePi:
    def __init__(self, args: list[str]) -> None:
        self.args = args
        self.home = Path(os.environ["PI_CODING_AGENT_DIR"])
        self.models = json.loads((self.home / "models.json").read_text())
        [(self.provider, self.config)] = self.models["providers"].items()
        self.model = self.config["models"][0]
        self.tools = args[args.index("--tools") + 1].split(",") if "--tools" in args else []
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.prompts: list[str] = []

    def credential(self) -> str:
        command = self.config["apiKey"]
        assert command.startswith("!"), command
        return subprocess.run(
            command[1:], shell=True, capture_output=True, text=True, check=True
        ).stdout

    def record(self) -> None:
        path = os.environ.get("FAKE_PI_RECORD")
        if path:
            Path(path).write_text(
                json.dumps(
                    {
                        "args": self.args,
                        "env": dict(os.environ),
                        "cwd": os.getcwd(),
                        "credential": self.credential(),
                        "models": self.models,
                        "settings": json.loads((self.home / "settings.json").read_text()),
                        "prompts": self.prompts,
                    }
                )
            )

    def emit(self, record: dict[str, Any]) -> None:
        with self.lock:
            sys.stdout.write(json.dumps(record, ensure_ascii=False) + "\n")
            sys.stdout.flush()

    def assistant(
        self, content: list[Any], stop_reason: str = "stop", error: str | None = None
    ) -> dict[str, Any]:
        message: dict[str, Any] = {
            "role": "assistant",
            "content": content,
            "provider": self.provider,
            "model": self.model["id"],
            "usage": {
                "input": 8,
                "output": 5,
                "cacheRead": 2,
                "cacheWrite": 0,
                "totalTokens": 15,
                "cost": {"total": 0.01},
            },
            "stopReason": stop_reason,
        }
        if error:
            message["errorMessage"] = error
        return message

    def finish(self, message: dict[str, Any]) -> None:
        self.emit({"type": "message_start", "message": {**message, "content": []}})
        self.emit({"type": "message_end", "message": message})
        self.emit({"type": "turn_end", "message": message, "toolResults": []})
        self.emit({"type": "agent_end", "messages": [message], "willRetry": False})
        self.emit({"type": "agent_settled"})

    def text(self, text: str) -> None:
        self.finish(self.assistant([{"type": "text", "text": text}]))

    def answer(self, text: str) -> None:
        self.emit({"type": "agent_start"})
        self.emit({"type": "turn_start"})
        if text == "fail":
            self.finish(self.assistant([], "error", "429 rate limited"))
        elif text == "slow":
            self.stop.clear()
            while not self.stop.wait(0.05):
                pass
            self.finish(self.assistant([{"type": "text", "text": "half"}], "aborted"))
        elif text.startswith("json"):
            self.text(f"Here:\n```json\n{text[4:].split(chr(10) * 2)[0].strip()}\n```")
        elif text.startswith("Your answer doesn't fit"):
            self.text(f"```json\n{os.environ.get('FAKE_PI_FIX', '{}')}\n```")
        elif text == "wide":
            delta = {"type": "text_delta", "delta": "x" * 200_000}
            self.emit({"type": "message_update", "assistantMessageEvent": delta})
            self.text("wide")
        else:
            if "ls" in self.tools:
                call = {
                    "type": "toolCall",
                    "id": "call_1",
                    "name": "ls",
                    "arguments": {"path": "."},
                }
                self.emit({"type": "message_end", "message": self.assistant([call], "toolUse")})
                self.emit(
                    {
                        "type": "tool_execution_start",
                        "toolCallId": "call_1",
                        "toolName": "ls",
                        "args": {"path": "."},
                    }
                )
                listing = "\n".join(sorted(os.listdir(".")))
                result = {"content": [{"type": "text", "text": listing}]}
                self.emit(
                    {
                        "type": "tool_execution_end",
                        "toolCallId": "call_1",
                        "toolName": "ls",
                        "result": result,
                        "isError": False,
                    }
                )
            self.text(f"you said: {text}\u2028.")

    def respond(self, ident: Any, kind: str, data: Any = None, error: str | None = None) -> None:
        response: dict[str, Any] = {
            "id": ident,
            "type": "response",
            "command": kind,
            "success": error is None,
        }
        if data is not None:
            response["data"] = data
        if error is not None:
            response["error"] = error
        self.emit(response)

    def serve(self) -> None:
        self.record()
        buffer = b""
        while chunk := os.read(0, 65536):
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                self.handle(json.loads(line))

    def handle(self, command: dict[str, Any]) -> None:
        kind, ident = command["type"], command.get("id")
        if kind == "get_state":
            self.respond(
                ident, kind, {"model": {"id": self.model["id"], "provider": self.provider}}
            )
        elif kind == "prompt":
            message = command["message"]
            if message == "exit":
                sys.stderr.write("fake pi: told to exit\n")
                sys.stderr.flush()
                os._exit(3)
            self.prompts.append(message)
            self.record()
            self.respond(ident, kind)
            threading.Thread(target=self.answer, args=(message,), daemon=True).start()
        elif kind == "abort":
            self.stop.set()
            self.respond(ident, kind)
        else:
            self.respond(ident, kind, error=f"Unknown command: {kind}")


def main() -> None:
    _FakePi(sys.argv[1:]).serve()


if __name__ == "__main__":
    main()
