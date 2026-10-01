import asyncio
import json
import time
from collections.abc import AsyncGenerator, Callable
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from google.adk.agents import LlmAgent
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.events.event_actions import EventActions
from google.adk.flows.llm_flows.functions import _extract_multimodal_parts
from google.adk.models import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import InMemoryRunner
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.function_tool import FunctionTool
from google.adk.tools.long_running_tool import LongRunningFunctionTool
from google.adk.tools.tool_confirmation import ToolConfirmation
from google.adk.tools.tool_context import ToolContext
from google.genai import types
from pydantic import Field

import forge_common.adk.toolset
from forge_common.adk import ForgeBaseToolset, ToolFailure, ToolTimeout


class Toolset(ForgeBaseToolset):
    def __init__(self, *tools: Callable[..., Any] | BaseTool, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.tools = [t if isinstance(t, BaseTool) else FunctionTool(t) for t in tools]

    async def get_raw_tools(
        self, readonly_context: ReadonlyContext | None = None
    ) -> list[BaseTool]:
        return self.tools


async def tools_of(toolset: ForgeBaseToolset) -> dict[str, BaseTool]:
    return {tool.name: tool for tool in await toolset.get_tools_with_prefix()}


def size(value: Any) -> int:
    """Characters of JSON, as it is sent: compact."""
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def context() -> Any:
    """A tool context for calling a tool directly: nothing asked of the user."""
    return MagicMock(actions=EventActions())


async def call(toolset: ForgeBaseToolset, tool_name: str, /, **args: Any) -> Any:
    tool = (await tools_of(toolset))[tool_name]
    return await tool.run_async(args=args, tool_context=context())


def get_repo(name: str) -> dict[str, Any]:
    """A repository."""
    return {"name": name, "stars": 3}


async def get_branch(name: str) -> dict[str, Any]:
    """A branch, which there never is."""
    raise KeyError(name)


def merge(branch: str) -> dict[str, Any]:
    """Merges a branch."""
    raise ToolFailure(
        f"{branch} has conflicts", suggested_fixes=["Rebase the branch, then merge it again."]
    )


def search(count: int) -> list[dict[str, Any]]:
    """Search results."""
    return [{"path": f"src/file_{i}.py", "line": i, "text": "match " * 10} for i in range(count)]


def read_file(size: int) -> str:
    """A file's text."""
    return "".join(f"line {i}\n" for i in range(size))


async def test_a_returned_value_is_the_payload_of_a_success() -> None:
    assert await call(Toolset(get_repo), "get_repo", name="forge") == {
        "status": "success",
        "payload": {"name": "forge", "stars": 3},
    }


async def test_a_raised_error_fails_with_its_reason_and_the_default_fixes() -> None:
    class Repos(Toolset):
        default_suggested_fixes = ("Call list_branches and use one of its names.",)

    assert await call(Repos(get_branch), "get_branch", name="main") == {
        "status": "failed",
        "reason": "KeyError: 'main'",
        "suggested_fixes": ["Call list_branches and use one of its names."],
    }


async def test_a_tool_failure_brings_its_own_reason_and_fixes() -> None:
    assert await call(Toolset(merge), "merge", branch="feature") == {
        "status": "failed",
        "reason": "feature has conflicts",
        "suggested_fixes": ["Rebase the branch, then merge it again."],
    }


async def test_suggest_fixes_sees_the_tool_under_its_own_name_and_the_call() -> None:
    seen = []

    class Repos(Toolset):
        def suggest_fixes(self, tool, args, error):
            seen.append((tool.name, args, type(error)))
            if isinstance(error, KeyError):
                return [f"There's no branch {args['name']}; list the branches first."]
            return super().suggest_fixes(tool, args, error)

    answer = await call(Repos(get_branch, tool_name_prefix="git"), "git_get_branch", name="dev")
    assert answer["suggested_fixes"] == ["There's no branch dev; list the branches first."]
    assert seen == [("get_branch", {"name": "dev"}, KeyError)]


async def test_a_suggest_fixes_that_raises_still_fails_the_call_cleanly() -> None:
    class Repos(Toolset):
        default_suggested_fixes = ("Try again.",)

        def suggest_fixes(self, tool, args, error):
            raise RuntimeError("bug")

    answer = await call(Repos(get_branch), "get_branch", name="main")
    assert answer["status"] == "failed"
    assert answer["suggested_fixes"] == ["Try again."]


@pytest.mark.parametrize(
    ("defaults", "fixes"),
    [(None, []), ("Try again.", ["Try again."]), (42, [])],
)
async def test_misconfigured_default_fixes_still_fail_the_call_cleanly(
    defaults: Any, fixes: list[str]
) -> None:
    class Repos(Toolset):
        default_suggested_fixes = defaults

    answer = await call(Repos(get_branch), "get_branch", name="main")
    assert answer == {"status": "failed", "reason": "KeyError: 'main'", "suggested_fixes": fixes}


async def test_a_suggest_fixes_that_returns_nothing_falls_back_to_the_defaults() -> None:
    class Repos(Toolset):
        default_suggested_fixes = ("Try again.",)

        def suggest_fixes(self, tool, args, error):
            if isinstance(error, ValueError):
                return ["Not this one."]
            # forgot: return super().suggest_fixes(tool, args, error)

    answer = await call(Repos(get_branch), "get_branch", name="main")
    assert answer["suggested_fixes"] == ["Try again."]


async def test_a_failure_that_cant_be_described_still_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(fixes: Any) -> list[str]:
        raise RuntimeError("bug in the guard")

    monkeypatch.setattr(forge_common.adk.toolset, "_as_fixes", broken)
    answer = await call(Toolset(get_branch), "get_branch", name="main")
    assert answer == {"status": "failed", "reason": "KeyError: 'main'", "suggested_fixes": []}


async def hang(seconds: float) -> dict[str, Any]:
    """Waits."""
    await asyncio.sleep(seconds)
    return {"waited": seconds}


async def test_a_call_past_its_timeout_fails_as_a_timeout() -> None:
    started = time.monotonic()
    answer = await call(Toolset(hang, timeout_seconds=0.05), "hang", seconds=30)

    assert time.monotonic() - started < 5
    assert answer["status"] == "failed"
    assert answer["reason"] == "The tool didn't finish within 0.05 seconds."
    assert answer["suggested_fixes"] == ToolTimeout(0.05).suggested_fixes


async def test_a_call_within_its_timeout_answers_as_usual() -> None:
    answer = await call(Toolset(hang, timeout_seconds=5), "hang", seconds=0.01)
    assert answer == {"status": "success", "payload": {"waited": 0.01}}


async def test_timeout_for_gives_each_tool_its_own_limit() -> None:
    async def clone(seconds: float) -> dict[str, Any]:
        """Clones slowly."""
        await asyncio.sleep(seconds)
        return {"cloned": True}

    class Git(Toolset):
        def timeout_for(self, tool):
            return None if tool.name == "clone" else super().timeout_for(tool)

    toolset = Git(hang, clone, timeout_seconds=0.05, tool_name_prefix="git")
    assert (await call(toolset, "git_clone", seconds=0.2))["status"] == "success"
    assert (await call(toolset, "git_hang", seconds=0.2))["status"] == "failed"


async def test_suggest_fixes_can_tell_a_timeout_apart() -> None:
    class Slow(Toolset):
        def suggest_fixes(self, tool, args, error):
            if isinstance(error, ToolTimeout):
                return [f"Ask for less than {args['seconds']} seconds of waiting."]
            return super().suggest_fixes(tool, args, error)

    answer = await call(Slow(hang, timeout_seconds=0.05), "hang", seconds=30)
    assert answer["suggested_fixes"] == ["Ask for less than 30 seconds of waiting."]


async def test_a_timeout_error_the_tool_raises_itself_is_not_the_toolsets_timeout() -> None:
    async def fetch(url: str) -> dict[str, Any]:
        """Fetches a URL."""
        raise TimeoutError("upstream took too long")

    answer = await call(Toolset(fetch, timeout_seconds=30), "fetch", url="https://example.com")
    assert answer["reason"] == "TimeoutError: upstream took too long"


def test_a_timeout_must_be_positive() -> None:
    with pytest.raises(ValueError, match="timeout_seconds"):
        Toolset(get_repo, timeout_seconds=0)


def lookup(key: str) -> dict[str, Any]:
    """Looks a key up, and says so when there's none."""
    return {"error": f"no key {key}"}


class Strict(Toolset):
    """Fails the calls whose result is an error."""

    def to_payload(self, tool, result):
        if isinstance(result, dict) and "error" in result:
            raise ToolFailure(result["error"], ["List the keys first."])
        return super().to_payload(tool, result)


async def test_to_payload_can_fail_a_tool_that_returns_its_errors() -> None:
    assert await call(Strict(lookup), "lookup", key="a") == {
        "status": "failed",
        "reason": "no key a",
        "suggested_fixes": ["List the keys first."],
    }


async def test_to_payload_leaves_a_call_waiting_on_the_user_alone() -> None:
    def deploy(tool_context: ToolContext) -> dict[str, Any]:
        """Deploys, once someone confirms it."""
        tool_context.actions.requested_tool_confirmations["call"] = ToolConfirmation(hint="Deploy?")
        return {"error": "This tool call requires confirmation."}

    assert await call(Strict(deploy), "deploy") == {
        "status": "success",
        "payload": {"error": "This tool call requires confirmation."},
    }


async def test_a_to_payload_that_breaks_says_the_tool_ran() -> None:
    class Broken(Toolset):
        def to_payload(self, tool, result):
            raise KeyError("bug")

    answer = await call(Broken(get_repo), "get_repo", name="forge")
    assert answer["status"] == "failed"
    assert answer["reason"].startswith("The tool ran, but its result couldn't be returned")


async def test_a_large_payload_is_cut_down_and_kept_whole_in_a_file(tmp_path: Path) -> None:
    toolset = Toolset(search, max_result_chars=2_000, result_dir=tmp_path)
    answer = await call(toolset, "search", count=500)

    assert answer["status"] == "success"
    assert answer["truncated"] is True
    assert size(answer["payload"]) <= 2_000
    # Its first results, whole, then how many it left out.
    kept = answer["payload"][:-1]
    assert len(kept) > 10 and kept == search(500)[: len(kept)]
    assert answer["payload"][-1] == f"… [{500 - len(kept)} more items]"

    location = Path(answer["complete_result_location"])
    assert location.parent == tmp_path
    assert location.name.startswith("search-") and location.suffix == ".json"
    assert json.loads(location.read_text()) == search(500)


async def test_long_strings_inside_a_payload_end_early(tmp_path: Path) -> None:
    def show(path: str) -> dict[str, Any]:
        """A file."""
        return {"path": path, "content": read_file(5_000)}

    answer = await call(
        Toolset(show, max_result_chars=1_000, result_dir=tmp_path), "show", path="a.py"
    )

    assert answer["payload"]["path"] == "a.py"
    content = answer["payload"]["content"]
    assert content.startswith("line 0\nline 1\n")
    assert content.endswith("more characters]")


async def test_every_one_of_a_few_long_records_keeps_the_start_of_its_text(tmp_path: Path) -> None:
    def show_all(paths: list[str]) -> list[dict[str, Any]]:
        """Files."""
        return [{"path": path, "content": read_file(5_000)} for path in paths]

    paths = [f"f{i}.py" for i in range(5)]
    answer = await call(
        Toolset(show_all, max_result_chars=2_000, result_dir=tmp_path), "show_all", paths=paths
    )

    assert size(answer["payload"]) <= 2_000
    assert [record["path"] for record in answer["payload"]] == paths
    assert all(record["content"].startswith("line 0\nline 1\n") for record in answer["payload"])


async def test_a_large_text_is_written_as_text(tmp_path: Path) -> None:
    answer = await call(
        Toolset(read_file, max_result_chars=500, result_dir=tmp_path), "read_file", size=10_000
    )

    assert answer["truncated"] is True
    assert size(answer["payload"]) <= 500
    location = Path(answer["complete_result_location"])
    assert location.suffix == ".txt"
    assert location.read_text() == read_file(10_000)


async def test_a_payload_within_the_limit_is_left_alone(tmp_path: Path) -> None:
    answer = await call(
        Toolset(search, max_result_chars=100_000, result_dir=tmp_path), "search", count=50
    )

    assert answer == {"status": "success", "payload": search(50)}
    assert list(tmp_path.iterdir()) == []


async def test_a_result_that_cant_be_written_is_still_cut_down(tmp_path: Path) -> None:
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("")
    answer = await call(
        Toolset(search, max_result_chars=1_000, result_dir=blocker), "search", count=500
    )

    assert answer["truncated"] is True
    assert answer["complete_result_location"] is None


async def test_close_deletes_the_complete_results(tmp_path: Path) -> None:
    toolset = Toolset(search, max_result_chars=1_000, result_dir=tmp_path)
    location = Path((await call(toolset, "search", count=500))["complete_result_location"])
    assert location.exists()

    await toolset.close()
    assert not location.exists()


async def test_a_toolset_that_cant_list_its_tools_gives_none() -> None:
    class Broken(ForgeBaseToolset):
        async def get_raw_tools(self, readonly_context=None):
            raise ConnectionError("server down")

    assert await Broken().get_tools() == []


def test_a_subclass_cant_bypass_the_guard_by_overriding_get_tools() -> None:
    with pytest.raises(TypeError, match="get_raw_tools"):

        class Unguarded(ForgeBaseToolset):
            async def get_tools(self, readonly_context=None):  # type: ignore[misc]
                return []


async def test_guarded_tools_keep_their_type_name_and_declaration_and_honour_the_filter() -> None:
    toolset = Toolset(get_repo, get_branch, tool_filter=["get_branch"], tool_name_prefix="git")
    (tool,) = (await tools_of(toolset)).values()

    assert isinstance(tool, FunctionTool)
    assert tool.name == "git_get_branch"
    declaration = tool._get_declaration()
    assert declaration is not None and declaration.name == "git_get_branch"
    answer = await tool.run_async(args={"name": "x"}, tool_context=context())
    assert answer["status"] == "failed"


async def test_tools_already_guarded_are_guarded_once() -> None:
    inner = Toolset(get_repo)

    class Outer(ForgeBaseToolset):
        async def get_raw_tools(self, readonly_context=None):
            return await inner.get_tools(readonly_context)

    assert await call(Outer(), "get_repo", name="forge") == {
        "status": "success",
        "payload": {"name": "forge", "stars": 3},
    }


async def test_adk_telemetry_sees_failures_and_the_tools_own_errors() -> None:
    tool = (await tools_of(Toolset(get_repo)))["get_repo"]
    detect = tool._detect_error_in_response  # type: ignore[attr-defined]

    assert detect({"status": "failed", "reason": "x", "suggested_fixes": []}) == "TOOL_ERROR"
    assert detect({"status": "success", "payload": {"error": "missing name"}}) == "TOOL_ERROR"
    assert detect({"status": "success", "payload": {"name": "forge"}}) is None


async def test_a_long_running_tool_that_returns_nothing_is_left_for_adk_to_answer() -> None:
    def start_build() -> None:
        """Starts a build that reports back later."""

    assert await call(Toolset(LongRunningFunctionTool(start_build)), "start_build") is None


async def test_media_stays_where_adk_takes_it_from() -> None:
    image = types.Part.from_bytes(data=b"\x89PNG", mime_type="image/png")

    def screenshot() -> dict[str, Any]:
        """Screenshots with a caption."""
        return {"caption": "the login page", "images": [image]}

    answer = await call(Toolset(screenshot, max_result_chars=100), "screenshot")
    remaining, parts = _extract_multimodal_parts(answer)

    assert remaining == {"status": "success", "payload": {"caption": "the login page"}}
    assert parts is not None and len(parts) == 1
    assert parts[0].inline_data is not None and parts[0].inline_data.data == b"\x89PNG"


class ScriptedLlm(BaseLlm):
    """Answers each model call with its next turn."""

    model: str = "scripted"
    turns: list[list[types.Part]] = Field(default_factory=list)
    requests: list[LlmRequest] = Field(default_factory=list)

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse]:
        self.requests.append(llm_request)
        yield LlmResponse(content=types.Content(role="model", parts=self.turns.pop(0)))


async def test_an_agent_reads_a_failed_call_instead_of_the_run_failing() -> None:
    llm = ScriptedLlm(
        turns=[
            [types.Part.from_function_call(name="git_get_branch", args={"name": "main"})],
            [types.Part.from_text(text="There's no main branch.")],
        ]
    )
    agent = LlmAgent(name="helper", model=llm, tools=[Toolset(get_branch, tool_name_prefix="git")])
    runner = InMemoryRunner(agent=agent, app_name="test")
    session = await runner.session_service.create_session(app_name="test", user_id="u")

    events = [
        event
        async for event in runner.run_async(
            user_id="u",
            session_id=session.id,
            new_message=types.Content(role="user", parts=[types.Part.from_text(text="Merge main")]),
        )
    ]

    responses = [r for e in events for r in e.get_function_responses()]
    assert [(r.name, r.response) for r in responses] == [
        (
            "git_get_branch",
            {"status": "failed", "reason": "KeyError: 'main'", "suggested_fixes": []},
        )
    ]
    assert events[-1].content is not None and events[-1].content.parts is not None
    assert events[-1].content.parts[0].text == "There's no main branch."
