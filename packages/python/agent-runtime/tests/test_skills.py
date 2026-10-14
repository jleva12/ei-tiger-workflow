"""Skills: SKILL.md folders read into bundles, and an agent's Skills tool built on ADK's SkillToolset."""

import copy
import io
import zipfile
from collections.abc import Sequence
from typing import Any

import pytest
from google.adk.agents import LlmAgent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools.skill_toolset import SkillToolset
from google.genai import types

from forge_agent_runtime.build import BuildError, build_app, build_tools
from forge_agent_runtime.code_execution import code_executor
from forge_agent_runtime.document import parse_document
from forge_agent_runtime.services import RuntimeServices
from forge_agent_runtime.skills import (
    MAX_FILE_BYTES,
    BundledSkills,
    SkillBundle,
    SkillError,
    SkillFile,
    bundle_of,
    find_skills,
    unzip,
)
from tests.conftest import EXAMPLE_HOSTS, ScriptedLlm, call, text

PDF_FORMS = """---
name: pdf-forms
description: Fills in PDF forms. Use when someone sends a PDF form to complete.
license: MIT
allowed-tools: Read Bash
version: 1.2
---

# Filling PDF forms

1. Read `forms.md` for the fields.
2. Run `scripts/fill.py`.
"""

REVIEW = """---
name: Code Review
description: Reviews a change against the team's checklist.
---
Check the tests first.
"""


def test_a_skill_md_is_read_into_a_bundle():
    skill = bundle_of(PDF_FORMS, [SkillFile.of("forms.md", b"# Fields")])
    assert skill.name == "pdf-forms"
    assert skill.description.startswith("Fills in PDF forms")
    assert skill.instructions.startswith("# Filling PDF forms")
    assert skill.license == "MIT" and skill.allowed_tools == "Read Bash"
    # Fields the spec doesn't name are kept, and written back.
    assert skill.extra == {"version": 1.2}
    again = bundle_of(skill.skill_md(), skill.files)
    assert again == skill


def test_a_name_that_isnt_kebab_case_is_made_one_and_a_missing_one_is_the_folders():
    assert bundle_of(REVIEW).name == "code-review"
    unnamed = "---\ndescription: Does a thing.\n---\nBody"
    assert bundle_of(unnamed, folder="skills/Release Notes").name == "release-notes"


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("# No frontmatter", "starts with YAML frontmatter"),
        ("---\n- a list\n---\n", "YAML mapping"),
        ("---\nname: x\n---\n", "no description"),
        ("---\nname: [unclosed\n---\n", "isn't YAML"),
    ],
)
def test_what_isnt_a_skill_says_why(text, reason):
    with pytest.raises(SkillError, match=reason):
        bundle_of(text)


def test_a_bundles_files_stay_in_its_folder_and_within_its_limits():
    with pytest.raises(ValueError, match="isn't a path"):
        SkillFile(path="../secrets.txt", content="x")
    with pytest.raises(ValueError, match="isn't a path"):
        SkillFile(path="/etc/passwd", content="x")
    assert SkillFile(path="references\\api.md", content="x").path == "references/api.md"
    with pytest.raises(ValueError, match="larger than"):
        SkillBundle(
            name="big", description="d", files=[SkillFile(path="a.txt", content="x" * (MAX_FILE_BYTES + 1))]
        )
    with pytest.raises(ValueError, match="same path"):
        SkillBundle(
            name="twice",
            description="d",
            files=[SkillFile(path="a.md", content="1"), SkillFile(path="a.md", content="2")],
        )
    with pytest.raises(ValueError, match="SKILL.md is its"):
        SkillBundle(name="itself", description="d", files=[SkillFile(path="SKILL.md", content="x")])
    binary = SkillFile.of("assets/logo.png", b"\x89PNG\r\n\x1a\n\x00")
    assert binary.encoding == "base64" and binary.data == b"\x89PNG\r\n\x1a\n\x00"


def test_a_folder_of_skills_is_read_skill_by_skill():
    found = find_skills(
        {
            "my-skills/pdf-forms/SKILL.md": PDF_FORMS.encode(),
            "my-skills/pdf-forms/forms.md": b"# Fields",
            "my-skills/pdf-forms/scripts/fill.py": b"print('filled')",
            "my-skills/pdf-forms/.DS_Store": b"\x00",
            "my-skills/review/skill.md": REVIEW.encode(),
            # A skill inside another is its own, with its own files.
            "my-skills/review/nested/SKILL.md": b"---\nname: nested\ndescription: Inner.\n---\nInner",
            "my-skills/review/nested/notes.md": b"inner notes",
            "my-skills/broken/SKILL.md": b"no frontmatter",
            "my-skills/README.md": b"not a skill's",
        }
    )
    skills = {skill.name: (root, skill) for root, skill in found.skills}
    assert set(skills) == {"pdf-forms", "code-review", "nested"}
    root, pdf = skills["pdf-forms"]
    assert root == "my-skills/pdf-forms"
    assert [file.path for file in pdf.files] == ["forms.md", "scripts/fill.py"]
    assert [file.path for file in skills["nested"][1].files] == ["notes.md"]
    assert skills["code-review"][1].files == []
    assert [(p.path, p.message) for p in found.problems] == [
        (
            "my-skills/broken/SKILL.md",
            "SKILL.md starts with YAML frontmatter between --- lines, naming the skill and describing it",
        )
    ]
    assert found.skipped == ["my-skills/README.md"]


def test_a_single_skill_md_is_a_skill_on_its_own():
    found = find_skills({"SKILL.md": PDF_FORMS.encode()})
    [(root, skill)] = found.skills
    assert root == "" and skill.name == "pdf-forms" and skill.files == []


def zipped(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for path, data in files.items():
            archive.writestr(path, data)
    return buffer.getvalue()


def test_an_archive_is_unpacked_with_its_paths():
    files = unzip(zipped({"pdf-forms/SKILL.md": PDF_FORMS.encode(), "pdf-forms/forms.md": b"# Fields"}))
    assert set(files) == {"pdf-forms/SKILL.md", "pdf-forms/forms.md"}
    [(_, skill)] = find_skills(files).skills
    assert skill.name == "pdf-forms"
    with pytest.raises(SkillError, match="isn't a zip"):
        unzip(b"plain text")


def test_adk_reads_every_file_including_those_outside_its_three_folders():
    skill = bundle_of(
        PDF_FORMS,
        [
            SkillFile.of("forms.md", b"# Fields"),
            SkillFile.of("references/api.md", b"api"),
            SkillFile.of("assets/template.txt", b"tpl"),
            SkillFile.of("scripts/fill.py", b"print(1)"),
        ],
    ).to_adk()
    assert skill.resources.get_reference("forms.md") == "# Fields"
    assert skill.resources.get_reference("api.md") == "api"
    assert skill.resources.get_asset("template.txt") == "tpl"
    script = skill.resources.get_script("fill.py")
    assert script is not None and script.src == "print(1)"
    # Its instructions end by listing them, as load_skill_resource takes them.
    assert "`references/forms.md` (the instructions call it `forms.md`)" in skill.instructions
    assert "`scripts/fill.py`" in skill.instructions


class Library:
    """The organization's skills, by ID."""

    def __init__(self, **skills: SkillBundle) -> None:
        self.skills = skills
        self.asked: list[tuple[list[str], str | None]] = []

    async def __call__(self, skill_ids: Sequence[str], *, organization_id: str | None) -> list[SkillBundle]:
        self.asked.append((list(skill_ids), organization_id))
        try:
            return [self.skills[skill_id] for skill_id in skill_ids]
        except KeyError as missing:
            raise LookupError(f"the organization has no skill {missing.args[0]}") from None


PDF = bundle_of(PDF_FORMS, [SkillFile.of("forms.md", b"# Fields")])
CODE_REVIEW = bundle_of(REVIEW)


def with_skills(example: dict[str, Any], *nodes: dict[str, Any]) -> dict[str, Any]:
    """The example, its agent with these Skills nodes."""
    doc = copy.deepcopy(example)
    for index, config in enumerate(nodes):
        node_id = f"skills_{index}"
        doc["nodes"].append({"id": node_id, "kind": "skills", "name": f"Skills {index}", "config": config})
        doc["edges"].append(
            {"id": f"agent:tools->{node_id}", "source": "agent", "source_output": "tools", "target": node_id}
        )
    return doc


async def toolset_of(app: Any) -> SkillToolset:
    root = app.root_agent
    assert isinstance(root, LlmAgent)
    [toolset] = [tool for tool in root.tools if isinstance(tool, SkillToolset)]
    return toolset


async def test_an_agents_skills_nodes_are_one_toolset_of_the_organizations_skills(example, models, web):
    library = Library(sk_pdf=PDF, sk_review=CODE_REVIEW)
    services = RuntimeServices(allowed_hosts=EXAMPLE_HOSTS, skills=library)
    doc = parse_document(with_skills(example, {"skills": ["sk_pdf"]}, {"skills": ["sk_review", "sk_pdf"]}))
    app = await build_app(doc, models=models, services=services, http=web())
    toolset = await toolset_of(app)
    assert [skill.name for skill in toolset.skills] == ["pdf-forms", "code-review"]
    assert library.asked == [(["sk_pdf", "sk_review"], doc.organization_id)]
    # The catalog goes in the instruction: there's no list_skills tool.
    assert "list_skills" not in {tool.name for tool in await toolset.get_tools()}


async def test_the_model_loads_a_skill_and_reads_its_files(example, models, web, llm: ScriptedLlm):
    services = RuntimeServices(allowed_hosts=EXAMPLE_HOSTS, skills=Library(sk_pdf=PDF))
    doc = with_skills(example, {"skills": ["sk_pdf"]})
    # The plain agent: just the skills.
    doc["nodes"] = [n for n in doc["nodes"] if n["kind"] in ("agent", "skills")]
    doc["edges"] = [e for e in doc["edges"] if e["target"].startswith("skills_")]
    app = await build_app(parse_document(doc), models=models, services=services, http=web())
    llm.turns = [
        [call("load_skill", skill_name="pdf-forms")],
        [call("load_skill_resource", skill_name="pdf-forms", file_path="references/forms.md")],
        [text("Done.")],
    ]
    sessions = InMemorySessionService()
    runner = Runner(app=app, session_service=sessions)
    session = await sessions.create_session(app_name=app.name, user_id="u")
    replies = []
    async for event in runner.run_async(
        user_id="u",
        session_id=session.id,
        new_message=types.Content(role="user", parts=[types.Part(text="Fill it")]),
    ):
        for part in (event.content.parts if event.content else None) or []:
            if part.function_response:
                replies.append(part.function_response.response)
    loaded, read = replies
    assert loaded is not None and loaded["instructions"].startswith("# Filling PDF forms")
    assert read is not None and read["content"] == "# Fields"
    # The model was told the skill up front, by name and description, and
    # offered no script runs: nothing here runs code.
    instruction = str(llm.requests[0].config.system_instruction)
    assert "pdf-forms" in instruction and "Fills in PDF forms" in instruction
    assert {"load_skill", "load_skill_resource"} <= set(llm.requests[0].tools_dict)
    assert "run_skill_script" not in llm.requests[0].tools_dict


async def test_its_scripts_run_only_where_it_says_so_and_code_runs(example, models, web):
    doc = parse_document(with_skills(example, {"skills": ["sk_pdf"], "run_scripts": True}))
    library = Library(sk_pdf=PDF)
    with pytest.raises(BuildError, match="code execution is off"):
        await build_app(
            doc,
            models=models,
            services=RuntimeServices(allowed_hosts=EXAMPLE_HOSTS, skills=library),
            http=web(),
        )
    services = RuntimeServices(
        allowed_hosts=EXAMPLE_HOSTS, skills=library, code_executor=code_executor("local")
    )
    toolset = await toolset_of(await build_app(doc, models=models, services=services, http=web()))
    assert "run_skill_script" in {tool.name for tool in await toolset.get_tools()}


async def test_skills_it_carries_need_no_library_and_others_do(example, models, web):
    doc = with_skills(example, {"skills": ["sk_pdf"]})
    doc["skills"] = {"sk_pdf": PDF.model_dump(mode="json")}
    toolset = await toolset_of(
        await build_app(
            parse_document(doc),
            models=models,
            services=RuntimeServices(allowed_hosts=EXAMPLE_HOSTS),
            http=web(),
        )
    )
    assert [skill.name for skill in toolset.skills] == ["pdf-forms"]

    unknown = with_skills(example, {"skills": ["sk_other"]})
    with pytest.raises(BuildError, match="only the hosted runtime has"):
        await build_app(
            parse_document(unknown),
            models=models,
            services=RuntimeServices(allowed_hosts=EXAMPLE_HOSTS),
            http=web(),
        )
    services = RuntimeServices(allowed_hosts=EXAMPLE_HOSTS, skills=Library())
    with pytest.raises(BuildError, match="no skill sk_other"):
        await build_app(parse_document(unknown), models=models, services=services, http=web())
    with pytest.raises(BuildError, match="which skills"):
        await build_app(
            parse_document(with_skills(example, {"skills": []})), models=models, services=services, http=web()
        )


async def test_two_skills_called_the_same_dont_build(example, models, web):
    services = RuntimeServices(allowed_hosts=EXAMPLE_HOSTS, skills=Library(a=PDF, b=PDF))
    with pytest.raises(BuildError, match="two skills called 'pdf-forms'"):
        await build_app(
            parse_document(with_skills(example, {"skills": ["a", "b"]})),
            models=models,
            services=services,
            http=web(),
        )


async def test_a_workflows_agent_gets_its_skills_from_what_the_run_carries(models):
    carried = BundledSkills({"sk_pdf": PDF.model_dump(mode="json")})
    [toolset] = await build_tools(
        "Skills", "skills", {"skills": ["sk_pdf"]}, models=models, services=RuntimeServices(skills=carried)
    )
    assert isinstance(toolset, SkillToolset) and [s.name for s in toolset.skills] == ["pdf-forms"]
    with pytest.raises(BuildError, match="no skill sk_gone"):
        await build_tools(
            "Skills",
            "skills",
            {"skills": ["sk_gone"]},
            models=models,
            services=RuntimeServices(skills=carried),
        )
