"""What a turn's tools act for: the agent whose turn runs, at the version it
was resolved to (``running_agent``), which a workflow's run records."""

from typing import Any

from google.genai import types

from forge_agent_runtime import (
    AgentExecutor,
    DocumentAgentSource,
    RunRequest,
    RuntimeServices,
    parse_document,
    running_agent,
)
from tests.conftest import PLAIN, call, text, without


class Workflows:
    """The organization's workflows: each run notes which agent, at which version, started it."""

    def __init__(self) -> None:
        self.started_by: list[tuple[str, Any] | None] = []

    async def describe(self, workflow_id, *, organization_id):
        return "Triage", "Sorts a ticket.", {}

    async def run(self, workflow_id, workflow_input, *, organization_id, context, wait=None):
        agent = running_agent()
        self.started_by.append(None if agent is None else (agent.document.id, agent.version))
        return {"run_id": "r1", "status": "running"}

    async def intend(self, starts, *, organization_id, context, source):
        raise AssertionError("No intake here.")

    async def dispatch(self, run_id, *, organization_id, wait):
        raise AssertionError("No intake here.")

    async def progress(self, **_: Any) -> dict[str, Any] | None:
        return None


async def test_a_turns_workflow_tool_runs_for_the_agent_and_version_that_runs(
    example, models, llm, web
) -> None:
    doc = without(example, *PLAIN)
    doc["version"] = 3
    doc["nodes"].append(
        {
            "id": "wf",
            "kind": "adk_workflow",
            "name": "Triage",
            "config": {"workflow": "ag_triage"},
            "outputs": [],
        }
    )
    doc["edges"].append({"id": "w", "source": "agent", "source_output": "tools", "target": "wf"})
    workflows = Workflows()
    agent = parse_document(doc)
    executor = AgentExecutor(
        DocumentAgentSource(agent), models=models, http=web(), services=RuntimeServices(workflows=workflows)
    )
    session = await executor.sessions.create_session(app_name=agent.id, user_id="ada")
    llm.turns = [[call("triage")], [text("It's being sorted.")]]

    request = RunRequest(
        app_name=agent.id,
        user_id="ada",
        session_id=session.id,
        new_message=types.Content(role="user", parts=[text("Sort my ticket")]),
    )
    [event async for event in executor.run(request)]

    assert workflows.started_by == [(agent.id, 3)]
    # Outside a turn, no agent runs.
    assert running_agent() is None
