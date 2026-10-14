"""An organization's skills vault, without MySQL: the tables on SQLite, the
organization's access in memory (the default grants), GitHub answered by a
fake. Writing, uploading and importing skills, reading a repository's, the
skill creator writing one in the background, and what runs reads of them.

The people: an organization admin, a member and a viewer of the
organization, and a member of another.
"""

import asyncio
import base64
import io
import json
import zipfile
from collections.abc import AsyncGenerator, AsyncIterator, Iterator
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import httpx2
import pytest
from casbin.persist.adapter import load_policy_line
from casbin.persist.adapters.asyncio import AsyncAdapter
from fastapi.testclient import TestClient
from forge_agent_runtime import SkillBundle
from forge_common.adk.usage import UsageCall
from google.adk.agents import LlmAgent
from google.adk.apps import App
from google.adk.artifacts import InMemoryArtifactService
from google.adk.models import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.sessions import InMemorySessionService
from google.genai import types
from pydantic import Field, SecretStr
from sqlalchemy import MetaData, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from forge_admin.adk_workflows.resources import RunResources
from forge_admin.adk_workflows.runs import AdkRunError, used_resources
from forge_admin.api.app import ROUTERS
from forge_admin.api.server import ApiServer
from forge_admin.assistant.runtime import AgentRuntime
from forge_admin.auth.authorization import get_enforcer, new_enforcer
from forge_admin.auth.tokens import mint_subject_token
from forge_admin.chat_agents.bundle import bundle
from forge_admin.config import Settings
from forge_admin.db.base import Base
from forge_admin.db.session import get_session
from forge_admin.instruction_writer.brief import Brief, render, resolve
from forge_admin.models import McpServer, Organization, Skill, SkillCreatorJob
from forge_admin.skills.vault import OrganizationSkills, SkillStamps, bundles_of

API = "/api/v1"
ORG = "3f6c0000-0000-4000-8000-000000000001"
OTHER_ORG = "3f6c0000-0000-4000-8000-000000000002"
ADMIN, MEMBER, VIEWER, NEIGHBOUR = "admin-1", "member-1", "viewer-1", "neighbour-1"
SKILLS = f"/organizations/{ORG}/skills"
JOBS = f"/organizations/{ORG}/skill-creator/jobs"

GRANTS = {
    "org:admin": ("organizations:read", "skills:manage"),
    "org:member": ("organizations:read", "skills:manage"),
    "org:viewer": ("organizations:read",),
}
ROLES = {
    ADMIN: ("org:admin", ORG),
    MEMBER: ("org:member", ORG),
    VIEWER: ("org:viewer", ORG),
    NEIGHBOUR: ("org:member", OTHER_ORG),
}

PDF_FORMS = """---
name: pdf-forms
description: Fills in PDF forms. Use when someone sends a PDF form to complete.
---

# Filling PDF forms

Read `forms.md`, then fill each field.
"""
REVIEW = """---
name: code-review
description: Reviews a change against the team's checklist.
---
Check the tests first.
"""


def skill(name: str = "triage", **fields: Any) -> dict[str, Any]:
    return {
        "name": name,
        "description": "Triages support tickets. Use when a ticket comes in.",
        "instructions": "1. Read the ticket.\n2. Tag it.",
        "files": [{"path": "references/tags.md", "content": "bug, billing, other"}],
    } | fields


class Grants(AsyncAdapter):
    def __init__(self) -> None:
        self.lines = [
            f"p, {role}, {permission.replace(':', ', ')}"
            for role, permissions in GRANTS.items()
            for permission in permissions
        ] + [f"g, {user}, {role}, org:{org}*" for user, (role, org) in ROLES.items()]

    async def load_policy(self, model: Any) -> None:
        for line in self.lines:
            load_policy_line(line, model)

    async def save_policy(self, model: Any) -> bool:
        return True

    async def add_policy(self, sec: str, ptype: str, rule: list[str]) -> None:
        pass

    async def remove_policy(self, sec: str, ptype: str, rule: list[str]) -> None:
        pass

    async def remove_filtered_policy(
        self, sec: str, ptype: str, field_index: int, *field_values: str
    ) -> None:
        pass


class Scripted(BaseLlm):
    """Answers each call with the next of ``turns``: parts, or an exception."""

    model: str = "scripted-model"
    turns: list[Any] = Field(default_factory=list)
    requests: list[LlmRequest] = Field(default_factory=list)

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse]:
        self.requests.append(llm_request)
        turn = self.turns.pop(0)
        if isinstance(turn, Exception):
            raise turn
        yield LlmResponse(content=types.Content(role="model", parts=turn))


def call(tool_name: str, /, **args: Any) -> types.Part:
    return types.Part(function_call=types.FunctionCall(name=tool_name, args=args))


def text(value: str) -> types.Part:
    return types.Part(text=value)


@dataclass
class Usage:
    calls: list[UsageCall] = field(default_factory=list)

    async def record(self, call: UsageCall) -> None:
        self.calls.append(call)


class GitHub:
    """github.com's API and raw files for one repository, acme/skills, at one commit."""

    COMMIT = "a" * 40

    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files
        self.requests: list[httpx2.Request] = []

    def _tree(self, prefix: str) -> list[dict[str, Any]]:
        entries: dict[str, dict[str, Any]] = {}
        for path, data in self.files.items():
            if not path.startswith(prefix):
                continue
            rest = path[len(prefix) :]
            head, _, tail = rest.partition("/")
            if tail:
                entries[head] = {
                    "path": head,
                    "type": "tree",
                    "sha": f"tree:{prefix}{head}/",
                }
            else:
                entries[head] = {
                    "path": head,
                    "type": "blob",
                    "sha": f"blob:{path}",
                    "size": len(data),
                }
        return list(entries.values())

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        url = str(request.url)
        repo = "https://api.github.com/repos/acme/skills"
        if url == repo:
            return httpx2.Response(200, json={"default_branch": "main"})
        if url == f"{repo}/commits/main":
            return httpx2.Response(
                200, json={"sha": self.COMMIT, "commit": {"tree": {"sha": "tree:"}}}
            )
        if url.startswith(f"{repo}/commits/"):
            return httpx2.Response(404)
        if url.startswith(f"{repo}/git/trees/"):
            sha = url.removeprefix(f"{repo}/git/trees/").split("?")[0]
            prefix = sha.removeprefix("tree:")
            if "recursive=1" in url:
                tree = [
                    {
                        "path": path[len(prefix) :],
                        "type": "blob",
                        "sha": f"blob:{path}",
                        "size": len(data),
                    }
                    for path, data in self.files.items()
                    if path.startswith(prefix)
                ]
                return httpx2.Response(200, json={"tree": tree, "truncated": False})
            return httpx2.Response(200, json={"tree": self._tree(prefix)})
        raw = f"https://raw.githubusercontent.com/acme/skills/{self.COMMIT}/"
        if url.startswith(raw):
            path = url.removeprefix(raw)
            if path in self.files:
                return httpx2.Response(200, content=self.files[path])
        return httpx2.Response(404)


@dataclass
class Workspace:
    client: TestClient
    sessions: async_sessionmaker[AsyncSession]
    tokens: dict[str, str]
    model: Scripted
    github: GitHub
    usage: Usage

    def call(
        self,
        method: str,
        path: str,
        user: str = MEMBER,
        body: Any = None,
        **more: Any,
    ) -> Any:
        return self.client.request(
            method,
            f"{API}{path}",
            headers={"Authorization": f"Bearer {self.tokens[user]}"},
            json=body,
            **more,
        )

    def add(self, user: str = MEMBER, **fields: Any) -> dict[str, Any]:
        made = self.call("POST", SKILLS, user, skill(**fields))
        assert made.status_code == 201, made.json()
        return dict(made.json())

    def upload(self, files: dict[str, bytes], *, paths: bool = True) -> Any:
        return self.call(
            "POST",
            f"{SKILLS}/read",
            files=[
                ("files", (name.split("/")[-1], data)) for name, data in files.items()
            ],
            data={"paths": json.dumps(list(files))} if paths else None,
        )

    def run(self, work: Any) -> Any:
        assert self.client.portal is not None
        return self.client.portal.call(work)

    def settle(self) -> None:
        """Wait for the skill creator's jobs to finish."""

        async def wait() -> None:
            app: Any = self.client.app
            tasks = list(getattr(app.state, "skill_creator_tasks", set()))
            if tasks:
                await asyncio.gather(*tasks)

        self.run(wait)


def sqlite_tables() -> MetaData:
    tables = MetaData()
    for name in (
        "organizations",
        "users",
        "skills",
        "skill_creator_jobs",
        "mcp_servers",
    ):
        table = Base.metadata.tables[name].to_metadata(tables)
        for column in table.columns:
            column.server_default = None
    return tables


@pytest.fixture
def workspace(settings: Settings) -> Iterator[Workspace]:
    configured = settings.model_copy(
        update={"jwt_secret": SecretStr("s" * 40), "local_user_id": None}
    )
    app = ApiServer(configured, routers=ROUTERS).create_app()
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    enforcer = new_enforcer(Grants())
    model = Scripted()
    runtime = AgentRuntime.of(
        [App(name="forge", root_agent=LlmAgent(name="forge", model=model))],
        sessions=InMemorySessionService(),
        artifacts=InMemoryArtifactService(),
    )
    usage = Usage()
    runtime.usage = usage  # type: ignore[assignment]
    app.state.agents = runtime
    github = GitHub(
        {
            "README.md": b"# Our skills",
            "skills/pdf-forms/SKILL.md": PDF_FORMS.encode(),
            "skills/pdf-forms/forms.md": b"# Fields",
            "skills/review/SKILL.md": REVIEW.encode(),
            "other/notes.txt": b"not a skill",
        }
    )
    app.state.github_transport = httpx2.MockTransport(github.handle)

    async def session() -> AsyncIterator[AsyncSession]:
        async with sessions() as made:
            yield made

    app.dependency_overrides[get_session] = session
    app.dependency_overrides[get_enforcer] = lambda: enforcer

    async def create() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(sqlite_tables().create_all)
        async with sessions() as made:
            made.add_all(
                [
                    Organization(id=ORG, name="Acme", description=""),
                    Organization(id=OTHER_ORG, name="Globex", description=""),
                    McpServer(
                        organization_id=ORG,
                        name="GitHub",
                        description="Acme's GitHub",
                        url="https://mcp.example.com",
                        tools=[
                            {"name": "create_issue", "description": "Opens an issue"}
                        ],
                    ),
                ]
            )
            await made.commit()

    tokens = {
        user: mint_subject_token(configured, user, lifetime=timedelta(minutes=5))
        for user in (ADMIN, MEMBER, VIEWER, NEIGHBOUR)
    }
    with TestClient(app) as client:
        assert client.portal is not None
        # The routes' background work uses the app's sessions: these.
        app.state.sessionmaker = sessions
        client.portal.call(create)
        yield Workspace(client, sessions, tokens, model, github, usage)
        client.portal.call(engine.dispose)


# ------------------------------------------------------------ the vault


def test_a_member_writes_a_skill_and_everyone_in_the_organization_reads_it(
    workspace: Workspace,
) -> None:
    made = workspace.add()
    assert made["name"] == "triage"
    assert made["source"] == {"kind": "written"}
    assert made["file_count"] == 1 and made["size_bytes"] > 0
    assert made["skill"]["files"] == [
        {
            "path": "references/tags.md",
            "content": "bug, billing, other",
            "encoding": "utf-8",
        }
    ]
    listed = workspace.call("GET", SKILLS, VIEWER).json()
    assert [s["name"] for s in listed] == ["triage"]
    # A listing leaves the skill's body out.
    assert "skill" not in listed[0]
    whole = workspace.call("GET", f"{SKILLS}/{made['id']}", VIEWER).json()
    assert whole["skill"]["instructions"].startswith("1. Read the ticket.")


def test_who_may_change_skills_and_what_isnt_one(workspace: Workspace) -> None:
    assert workspace.call("POST", SKILLS, VIEWER, skill()).status_code == 403
    assert workspace.call("GET", SKILLS, NEIGHBOUR).status_code == 403
    assert (
        workspace.call("POST", SKILLS, body=skill(name="Not Kebab")).status_code == 422
    )
    escaping = skill(files=[{"path": "../x", "content": "x"}])
    assert workspace.call("POST", SKILLS, body=escaping).status_code == 422
    workspace.add()
    taken = workspace.call("POST", SKILLS, body=skill())
    assert taken.status_code == 409
    assert "already has a skill by that name" in taken.json()["detail"]


def test_a_skill_is_changed_whole_downloaded_and_deleted(workspace: Workspace) -> None:
    made = workspace.add()
    path = f"{SKILLS}/{made['id']}"
    changed = workspace.call(
        "PUT", path, body=skill(description="Sorts tickets.", files=[])
    )
    assert changed.status_code == 200
    assert changed.json()["skill"]["description"] == "Sorts tickets."
    assert changed.json()["file_count"] == 0

    download = workspace.call("GET", f"{path}/download", VIEWER)
    assert (
        download.headers["content-disposition"] == 'attachment; filename="triage.zip"'
    )
    archive = zipfile.ZipFile(io.BytesIO(download.content))
    assert archive.namelist() == ["triage/SKILL.md"]
    assert archive.read("triage/SKILL.md").decode().startswith("---\nname: triage\n")

    assert workspace.call("DELETE", path, VIEWER).status_code == 403
    assert workspace.call("DELETE", path).status_code == 204
    assert workspace.call("GET", path).status_code == 404
    # Another organization's people never see it.
    other = workspace.add()
    elsewhere = f"/organizations/{OTHER_ORG}/skills/{other['id']}"
    assert workspace.call("GET", elsewhere, NEIGHBOUR).status_code == 404


# ------------------------------------------------------------ uploads


def test_a_folder_of_skills_is_read_then_the_chosen_ones_imported(
    workspace: Workspace,
) -> None:
    workspace.add(name="code-review")
    read = workspace.upload(
        {
            "mine/pdf-forms/SKILL.md": PDF_FORMS.encode(),
            "mine/pdf-forms/forms.md": b"# Fields",
            "mine/pdf-forms/logo.png": b"\x89PNG\r\n\x1a\n\x00",
            "mine/review/SKILL.md": REVIEW.encode(),
            "mine/broken/SKILL.md": b"no frontmatter",
            "mine/README.md": b"notes",
        }
    )
    assert read.status_code == 200, read.json()
    found = {s["skill"]["name"]: s for s in read.json()["skills"]}
    assert set(found) == {"pdf-forms", "code-review"}
    assert found["pdf-forms"]["folder"] == "mine/pdf-forms"
    assert found["pdf-forms"]["source"] == {
        "kind": "upload",
        "folder": "mine/pdf-forms",
    }
    assert found["pdf-forms"]["existing"] is None
    # The same name as one of the organization's: adding it would replace it.
    assert found["code-review"]["existing"] is not None
    files = {f["path"]: f for f in found["pdf-forms"]["skill"]["files"]}
    assert files["logo.png"]["encoding"] == "base64"
    assert read.json()["problems"][0]["path"] == "mine/broken/SKILL.md"
    assert read.json()["skipped"] == ["mine/README.md"]

    # Adding one under a name that's taken, without replacing it, is refused.
    clash = workspace.call(
        "POST",
        f"{SKILLS}/import",
        body={"skills": [{"skill": found["code-review"]["skill"]}]},
    )
    assert clash.status_code == 409
    imported = workspace.call(
        "POST",
        f"{SKILLS}/import",
        body={
            "skills": [
                {
                    "skill": found["pdf-forms"]["skill"],
                    "source": found["pdf-forms"]["source"],
                },
                {
                    "skill": found["code-review"]["skill"],
                    "source": found["code-review"]["source"],
                    "replace": found["code-review"]["existing"],
                },
            ]
        },
    )
    assert imported.status_code == 200, imported.json()
    listed = {s["name"]: s for s in workspace.call("GET", SKILLS).json()}
    assert set(listed) == {"pdf-forms", "code-review"}
    assert listed["code-review"]["id"] == found["code-review"]["existing"]
    assert listed["code-review"]["source"] == {
        "kind": "upload",
        "folder": "mine/review",
    }


def test_a_zip_or_a_single_skill_md_is_read_too(workspace: Workspace) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("pdf-forms/SKILL.md", PDF_FORMS)
        archive.writestr("pdf-forms/forms.md", "# Fields")
    zipped = workspace.upload({"pdf-forms.skill": buffer.getvalue()})
    assert [s["skill"]["name"] for s in zipped.json()["skills"]] == ["pdf-forms"]

    single = workspace.upload({"review.md": REVIEW.encode()}, paths=False)
    [found] = single.json()["skills"]
    assert found["skill"]["name"] == "code-review" and found["folder"] == "review"

    broken = workspace.upload({"x.zip": b"PK\x03\x04 nope"})
    assert broken.status_code == 422
    refused = workspace.call(
        "POST", f"{SKILLS}/read", VIEWER, files=[("files", ("a", b""))]
    )
    assert refused.status_code == 403


# ------------------------------------------------------------ GitHub


def test_a_repositorys_skills_are_read_from_a_folder_url_and_pulled_again(
    workspace: Workspace,
) -> None:
    read = workspace.call(
        "POST",
        f"{SKILLS}/github",
        body={"url": "https://github.com/acme/skills/tree/main/skills"},
    )
    assert read.status_code == 200, read.json()
    found = {s["skill"]["name"]: s for s in read.json()["skills"]}
    assert set(found) == {"pdf-forms", "code-review"}
    assert found["pdf-forms"]["source"] == {
        "kind": "github",
        "repository": "https://github.com/acme/skills",
        "ref": "main",
        "commit": GitHub.COMMIT,
        "path": "skills/pdf-forms",
    }
    assert [f["path"] for f in found["pdf-forms"]["skill"]["files"]] == ["forms.md"]
    # Only the skills' files were fetched, and from the commit.
    fetched = [
        str(r.url)
        for r in workspace.github.requests
        if "raw.githubusercontent" in str(r.url)
    ]
    assert sorted(fetched) == sorted(
        f"https://raw.githubusercontent.com/acme/skills/{GitHub.COMMIT}/skills/{path}"
        for path in ("pdf-forms/SKILL.md", "pdf-forms/forms.md", "review/SKILL.md")
    )

    imported = workspace.call(
        "POST",
        f"{SKILLS}/import",
        body={
            "skills": [
                {
                    "skill": found["pdf-forms"]["skill"],
                    "source": found["pdf-forms"]["source"],
                }
            ]
        },
    ).json()
    skill_id = imported[0]["id"]

    workspace.github.files["skills/pdf-forms/SKILL.md"] = PDF_FORMS.replace(
        "Read `forms.md`", "Read `forms.md` twice"
    ).encode()
    pulled = workspace.call("POST", f"{SKILLS}/{skill_id}/pull")
    assert pulled.status_code == 200, pulled.json()
    assert "twice" in pulled.json()["skill"]["instructions"]

    written = workspace.add()
    assert workspace.call("POST", f"{SKILLS}/{written['id']}/pull").status_code == 409


def test_what_github_cant_show_says_why(workspace: Workspace) -> None:
    def read(**body: Any) -> Any:
        return workspace.call("POST", f"{SKILLS}/github", body=body)

    assert read(url="https://gitlab.com/acme/skills").status_code == 422
    missing = read(url="https://github.com/acme/skills", path="nowhere")
    assert (
        missing.status_code == 404 and "no folder nowhere" in missing.json()["detail"]
    )
    assert read(url="https://github.com/acme/skills", ref="v9").status_code == 404
    private = read(url="https://github.com/acme/secret")
    assert (
        private.status_code == 404 and "pick a credential" in private.json()["detail"]
    )
    # The repository's README isn't a skill's: nothing's found outside skills' folders.
    other = read(url="acme/skills", path="other")
    assert other.status_code == 200 and other.json()["skills"] == []


# ------------------------------------------------------------ the creator


def test_the_creator_writes_a_skill_in_the_background_from_what_the_org_has(
    workspace: Workspace,
) -> None:
    workspace.model.turns = [
        [call("list_mcp_servers")],
        [
            call(
                "save_skill",
                name="Bad Name",
                description="x",
                instructions="Steps",
            )
        ],
        [
            call(
                "save_skill",
                name="open-bug-reports",
                description="Opens a bug report. Use when someone reports a bug.",
                instructions="Needs: the GitHub server's `create_issue` tool.\n1. Ask what broke.",
                files=[
                    {
                        "path": "references/template.md",
                        "content": "## Steps to reproduce",
                    }
                ],
                notes=["Give the agent the GitHub MCP server."],
            )
        ],
        [text("Made open-bug-reports.")],
    ]
    started = workspace.call(
        "POST", JOBS, body={"brief": "A skill to open bug reports"}
    )
    assert started.status_code == 202, started.json()
    assert started.json()["status"] == "running"
    workspace.settle()

    [job] = workspace.call("GET", JOBS, VIEWER).json()
    assert job["status"] == "done", job["error"]
    assert job["notes"] == ["Give the agent the GitHub MCP server."]
    made = workspace.call("GET", f"{SKILLS}/{job['skill_id']}").json()
    assert made["name"] == "open-bug-reports"
    assert made["source"] == {"kind": "creator", "job": job["id"]}
    assert made["skill"]["files"][0]["path"] == "references/template.md"
    # It looked at the organization's servers, and was told what was wrong.
    replies = [
        part.function_response.response
        for content in workspace.model.requests[-1].contents
        for part in content.parts or ()
        if part.function_response
    ]
    assert replies[0]["mcp_servers"][0]["tools"][0]["name"] == "create_issue"  # type: ignore[index]
    assert replies[1]["saved"] is False  # type: ignore[index]
    # Its calls count as the assistant's.
    assert workspace.usage.calls

    # A dismissed job is gone; the skill stays.
    assert workspace.call("DELETE", f"{JOBS}/{job['id']}").status_code == 204
    assert workspace.call("GET", JOBS).json() == []


def test_the_creator_reworks_a_skill_and_keeps_its_name_free(
    workspace: Workspace,
) -> None:
    first = workspace.add(name="triage")
    workspace.add(name="sorting")
    workspace.model.turns = [
        [
            call(
                "save_skill",
                name="sorting",
                description="Triages tickets by urgency.",
                instructions="Sort by urgency.",
            )
        ],
        [text("Done.")],
    ]
    started = workspace.call(
        "POST", JOBS, body={"brief": "Sort by urgency", "revises": first["id"]}
    )
    assert started.status_code == 202
    workspace.settle()
    [job] = workspace.call("GET", JOBS).json()
    assert job["skill_id"] == first["id"]
    reworked = workspace.call("GET", f"{SKILLS}/{first['id']}").json()
    # "sorting" is another skill's: it's given a name of its own.
    assert reworked["name"] == "sorting-2"
    assert reworked["skill"]["instructions"] == "Sort by urgency."
    # The model was sent the skill to rework.
    sent = "".join(
        p.text or ""
        for c in workspace.model.requests[0].contents
        for p in c.parts or ()
    )
    assert "The skill to rework" in sent and "name: triage" in sent


def test_a_creator_that_saves_nothing_fails_saying_so(workspace: Workspace) -> None:
    workspace.model.turns = [[text("I can't.")]]
    workspace.call("POST", JOBS, body={"brief": "Anything"})
    workspace.settle()
    [job] = workspace.call("GET", JOBS).json()
    assert job["status"] == "failed"
    assert "without saving a skill" in job["error"]
    assert workspace.call("POST", JOBS, VIEWER, body={"brief": "x"}).status_code == 403


def test_a_job_left_running_by_a_restart_is_said_to_have_stopped(
    workspace: Workspace,
) -> None:
    async def old_job() -> None:
        async with workspace.sessions() as session:
            job = SkillCreatorJob(
                organization_id=ORG, brief="x", status="running", notes=[]
            )
            session.add(job)
            await session.flush()
            job.created_at = job.created_at - timedelta(hours=1)
            await session.commit()

    workspace.run(old_job)
    [job] = workspace.call("GET", JOBS).json()
    assert job["status"] == "failed" and "restarted" in job["error"]


# ------------------------------------------------------------ what runs reads


def test_what_runs_reads_is_the_organizations_skills_whole(
    workspace: Workspace,
) -> None:
    made = workspace.add()
    theirs = workspace.call(
        "POST", f"/organizations/{OTHER_ORG}/skills", NEIGHBOUR, skill(name="secret")
    ).json()
    library = OrganizationSkills(workspace.sessions)

    async def read() -> None:
        [found] = await library([made["id"]], organization_id=ORG)
        assert isinstance(found, SkillBundle) and found.name == "triage"
        with pytest.raises(LookupError, match="no skill"):
            await library([theirs["id"]], organization_id=ORG)
        async with workspace.sessions() as session:
            assert set(await bundles_of(session, ORG, [made["id"], theirs["id"]])) == {
                made["id"]
            }

    workspace.run(read)


def agent_with_skills(*ids: str) -> dict[str, Any]:
    return {
        "format": "forge.chat_agent/v1",
        "id": "ca_skilled01",
        "name": "Skilled",
        "organization_id": ORG,
        "nodes": [
            {"id": "agent", "kind": "agent", "name": "Helper", "config": {}},
            {
                "id": "skills_1",
                "kind": "skills",
                "name": "Skills",
                "config": {"skills": list(ids)},
            },
        ],
        "edges": [
            {
                "id": "e",
                "source": "agent",
                "source_output": "tools",
                "target": "skills_1",
            }
        ],
    }


def test_an_exported_agent_carries_its_skills_and_its_cache_key_their_changes(
    workspace: Workspace,
) -> None:
    made = workspace.add()
    stamps = SkillStamps(workspace.sessions)

    async def find(ids: Any) -> Any:
        async with workspace.sessions() as session:
            return await bundles_of(session, ORG, ids)

    async def check() -> tuple[str, str]:
        document, notes = await bundle(
            None,  # type: ignore[arg-type]
            ORG,
            agent_with_skills(made["id"], "gone"),
            skills=find,
        )
        assert set(document["skills"]) == {made["id"]}
        assert document["skills"][made["id"]]["name"] == "triage"
        assert notes == ["it uses a skill that was deleted"]
        _, unbundled = await bundle(None, ORG, agent_with_skills(made["id"]))  # type: ignore[arg-type]
        assert unbundled == [
            "its Skills tools use the organization's skills, which aren't bundled"
        ]
        assert await stamps(ORG, agent_with_skills()) == ""
        return await stamps(ORG, agent_with_skills(made["id"])), ""

    before, _ = workspace.run(check)
    assert before.startswith("1:")
    workspace.call("PUT", f"{SKILLS}/{made['id']}", body=skill(description="Changed."))

    async def again() -> str:
        return await stamps(ORG, agent_with_skills(made["id"]))

    assert workspace.run(again) != before


def test_a_workflow_run_carries_the_skills_its_agents_use(workspace: Workspace) -> None:
    made = workspace.add()
    resources = RunResources(ORG, chat_agents=None, sessions=workspace.sessions)

    def workflow(*ids: str) -> dict[str, Any]:
        return {
            "nodes": [
                {
                    "id": "helper",
                    "kind": "llm",
                    "name": "Helper",
                    "config": {
                        "tools": [
                            {
                                "id": "t",
                                "kind": "skills",
                                "name": "Skills",
                                "config": {"skills": list(ids)},
                            }
                        ]
                    },
                }
            ]
        }

    async def check() -> None:
        _, _, skills = await used_resources([workflow(made["id"])], resources)
        assert skills[made["id"]]["name"] == "triage"
        with pytest.raises(AdkRunError, match="no skill gone"):
            await used_resources([workflow("gone")], resources)

    workspace.run(check)


def test_the_instruction_writer_is_told_an_agents_skills(workspace: Workspace) -> None:
    made = workspace.add()
    brief = Brief.model_validate(
        {
            "where": "chat",
            "role": "chat_agent",
            "name": "Helper",
            "tools": [
                {
                    "kind": "skills",
                    "name": "Skills",
                    "config": {"skills": [made["id"]], "run_scripts": False},
                }
            ],
        }
    )

    async def check() -> str:
        async with workspace.sessions() as session:
            return render(brief, await resolve(brief, ORG, session))

    rendered = workspace.run(check)
    assert "`load_skill(skill_name)`" in rendered
    assert "- `triage`: Triages support tickets." in rendered


def test_skills_are_kept_whole_on_their_rows(workspace: Workspace) -> None:
    binary = base64.b64encode(b"\x00\x01").decode()
    made = workspace.add(
        files=[{"path": "assets/x.bin", "content": binary, "encoding": "base64"}]
    )

    async def row() -> Skill:
        async with workspace.sessions() as session:
            found = await session.scalar(select(Skill).where(Skill.id == made["id"]))
            assert found is not None
            return found

    found = workspace.run(row)
    assert found.size_bytes == len(b"1. Read the ticket.\n2. Tag it.") + 2
    assert found.file_count == 1
