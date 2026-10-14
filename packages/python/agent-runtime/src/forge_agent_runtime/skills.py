"""Skills: folders of instructions an agent loads when a task calls for them
(Agent Skills, https://agentskills.io): a ``SKILL.md`` (YAML frontmatter
naming the skill and saying when to use it, then Markdown instructions) and
the files beside it (references, assets, scripts).

A skill travels as a :class:`SkillBundle`, the one shape every part of Forge
reads and writes: the organization's skills vault keeps them, uploads and
GitHub imports are read into them (:func:`find_skills`), the skill creator
writes them, exported agents and workflow runs carry them, and an agent's
Skills tool (a ``skills`` node) is built from them on ADK's
``SkillToolset`` (:func:`skill_toolset`). The model sees each skill's name
and description from the start and loads its instructions (``load_skill``)
and files (``load_skill_resource``) when it needs them; it runs a skill's
scripts (``run_skill_script``) only where the tool allows it and the
deployment runs code.
"""

from __future__ import annotations

import base64
import io
import posixpath
import re
import zipfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

if TYPE_CHECKING:
    from google.adk.code_executors.base_code_executor import BaseCodeExecutor
    from google.adk.skills import Skill
    from google.adk.tools.skill_toolset import SkillToolset

#: A skill's name: lowercase words joined by hyphens, as the spec has it.
NAME_PATTERN = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
MAX_NAME = 64
MAX_DESCRIPTION = 1024
#: The most a skill's instructions (its SKILL.md body) may be, in characters.
MAX_INSTRUCTIONS = 200_000
#: The most one of its files may be, in bytes.
MAX_FILE_BYTES = 1_000_000
#: The most a whole skill may be: its instructions and its files, in bytes.
MAX_SKILL_BYTES = 4_000_000
#: The most files a skill may have beside its SKILL.md.
MAX_FILES = 200
#: The most an archive may unpack to, in bytes, and how many entries it may have.
MAX_ARCHIVE_BYTES = 50_000_000
MAX_ARCHIVE_ENTRIES = 5_000
#: The folders ADK reads a skill's files from; a file anywhere else is read
#: as a reference at its own path.
RESOURCE_FOLDERS = ("references/", "assets/", "scripts/")
#: The frontmatter fields the spec names; any others are kept as they are.
_KNOWN_FIELDS = {
    "name",
    "description",
    "license",
    "compatibility",
    "allowed-tools",
    "allowed_tools",
    "metadata",
}
#: Files and folders an upload or a repository has that aren't the skill's.
_IGNORED = {".git", ".github", "__pycache__", "node_modules", ".DS_Store", "Thumbs.db", ".venv"}


class SkillError(ValueError):
    """A skill that can't be read as it is, and why."""


class SkillFile(BaseModel):
    """
    One of a skill's files, at its path in the skill's folder.

    :ivar content: Its text; base64 for a file that isn't UTF-8 text.
    """

    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=300)
    content: str
    encoding: Literal["utf-8", "base64"] = "utf-8"

    @field_validator("path")
    @classmethod
    def _relative(cls, path: str) -> str:
        return clean_path(path)

    @property
    def data(self) -> bytes:
        """Its bytes."""
        return base64.b64decode(self.content) if self.encoding == "base64" else self.content.encode("utf-8")

    @property
    def size(self) -> int:
        return len(self.data) if self.encoding == "base64" else len(self.content.encode("utf-8"))

    @classmethod
    def of(cls, path: str, data: bytes) -> SkillFile:
        """:return: The file: text when it's UTF-8, base64 otherwise."""
        try:
            return cls(path=path, content=data.decode("utf-8"))
        except UnicodeDecodeError:
            return cls(path=path, content=base64.b64encode(data).decode("ascii"), encoding="base64")


class SkillBundle(BaseModel):
    """
    A skill, whole: what its ``SKILL.md`` says and the files beside it.

    :ivar instructions: The ``SKILL.md`` body: what the model reads when it
        loads the skill.
    :ivar extra: Frontmatter fields the spec doesn't name, kept as they are.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=MAX_NAME)
    description: str = Field(min_length=1, max_length=MAX_DESCRIPTION)
    instructions: str = Field(default="", max_length=MAX_INSTRUCTIONS)
    license: str | None = Field(default=None, max_length=500)
    compatibility: str | None = Field(default=None, max_length=500)
    allowed_tools: str | None = Field(default=None, max_length=2000)
    metadata: dict[str, Any] = Field(default_factory=dict)
    extra: dict[str, Any] = Field(default_factory=dict)
    files: list[SkillFile] = Field(default_factory=list, max_length=MAX_FILES)

    @field_validator("name")
    @classmethod
    def _kebab(cls, name: str) -> str:
        if not NAME_PATTERN.fullmatch(name):
            raise ValueError(
                "a skill's name is lowercase letters and digits joined by single hyphens, e.g. pdf-forms"
            )
        return name

    @field_validator("description")
    @classmethod
    def _said(cls, description: str) -> str:
        if not description.strip():
            raise ValueError("say what the skill does and when to use it")
        return description.strip()

    @model_validator(mode="after")
    def _fits(self) -> SkillBundle:
        paths = [file.path for file in self.files]
        if len(set(paths)) != len(paths):
            raise ValueError("two of its files have the same path")
        for file in self.files:
            if file.path.lower() == "skill.md":
                raise ValueError(
                    "its SKILL.md is its name, description and instructions, not one of its files"
                )
            if file.size > MAX_FILE_BYTES:
                raise ValueError(f"{file.path} is larger than {MAX_FILE_BYTES // 1_000_000} MB")
        if self.size > MAX_SKILL_BYTES:
            raise ValueError(
                f"it's larger than {MAX_SKILL_BYTES // 1_000_000} MB, its files and instructions together"
            )
        return self

    @property
    def size(self) -> int:
        """Its instructions' and files' bytes."""
        return len(self.instructions.encode("utf-8")) + sum(file.size for file in self.files)

    def skill_md(self) -> str:
        """:return: Its ``SKILL.md``, as a folder of it has it."""
        return render_skill_md(self)

    def to_adk(self) -> Skill:
        """:return: The skill as ADK's ``SkillToolset`` reads one."""
        return adk_skill(self)


@dataclass(frozen=True)
class SkillProblem:
    """A ``SKILL.md`` found that isn't a skill as it is: where, and why."""

    path: str
    message: str


@dataclass
class FoundSkills:
    """
    What a folder, an archive or a repository holds.

    :ivar skills: Each skill, with the folder it's in (empty for the top).
    :ivar problems: Each SKILL.md that couldn't be read.
    :ivar skipped: Files that aren't any skill's (outside every skill's folder).
    """

    skills: list[tuple[str, SkillBundle]] = field(default_factory=list)
    problems: list[SkillProblem] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def clean_path(path: str) -> str:
    """
    :return: A file's path in a skill's folder, as POSIX, relative and normal.
    :raises ValueError: It leaves the folder, or isn't a file's path.
    """
    text = path.replace("\\", "/").strip()
    if not text or text.startswith("/") or re.match(r"^[A-Za-z]:", text):
        raise ValueError(f"{path!r} isn't a path in the skill's folder")
    parts = text.split("/")
    if any(part in ("", ".", "..") for part in parts) or any(ord(c) < 32 for c in text):
        raise ValueError(f"{path!r} isn't a path in the skill's folder")
    return posixpath.normpath(text)


def slug(text: str) -> str:
    """:return: ``text`` as a skill's name: lowercase words joined by hyphens."""
    words = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return re.sub(r"-{2,}", "-", words)[:MAX_NAME].strip("-")


def parse_skill_md(text: str) -> tuple[dict[str, Any], str]:
    """
    :return: A ``SKILL.md``'s frontmatter and its body.
    :raises SkillError: It has no YAML frontmatter, or it isn't a mapping.
    """
    text = text.lstrip("﻿")
    match = re.match(r"^---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|$)(.*)$", text, re.DOTALL)
    if match is None:
        raise SkillError(
            "SKILL.md starts with YAML frontmatter between --- lines, naming the skill and describing it"
        )
    try:
        frontmatter = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as error:
        raise SkillError(f"SKILL.md's frontmatter isn't YAML: {error}") from None
    if not isinstance(frontmatter, dict):
        raise SkillError("SKILL.md's frontmatter is a YAML mapping (name: …, description: …)")
    return {str(key): value for key, value in frontmatter.items()}, match.group(2).strip()


def bundle_of(text: str, files: Iterable[SkillFile] = (), *, folder: str = "") -> SkillBundle:
    """
    :param text: A ``SKILL.md``.
    :param files: The files beside it.
    :param folder: Its folder's name: the skill's name when its frontmatter has none.
    :return: The skill. A name that isn't lowercase words joined by hyphens
        ("PDF Forms") is made one (``pdf-forms``).
    :raises SkillError: It isn't a skill as it is, saying why.
    """
    frontmatter, body = parse_skill_md(text)
    name = slug(str(frontmatter.get("name") or "")) or slug(posixpath.basename(folder))
    if not name:
        raise SkillError("SKILL.md's frontmatter has no name")
    description = frontmatter.get("description")
    if not isinstance(description, str) or not description.strip():
        raise SkillError(
            "SKILL.md's frontmatter has no description: say what the skill does and when to use it"
        )
    metadata = frontmatter.get("metadata")
    allowed = frontmatter.get("allowed-tools", frontmatter.get("allowed_tools"))
    if isinstance(allowed, list):
        allowed = " ".join(str(tool) for tool in allowed)
    try:
        return SkillBundle(
            name=name,
            description=" ".join(description.split())[:MAX_DESCRIPTION],
            instructions=body,
            license=_text(frontmatter.get("license")),
            compatibility=_text(frontmatter.get("compatibility")),
            allowed_tools=_text(allowed),
            metadata=metadata if isinstance(metadata, dict) else {},
            extra={key: value for key, value in frontmatter.items() if key not in _KNOWN_FIELDS},
            files=list(files),
        )
    except ValueError as error:
        raise SkillError(_reason(error)) from None


def _text(value: Any) -> str | None:
    return None if value is None or value == "" else str(value)


def _reason(error: ValueError) -> str:
    """:return: What a validation error says, without pydantic's framing."""
    errors: Any = getattr(error, "errors", None)
    if callable(errors):
        found: list[dict[str, Any]] = errors()
        if found:
            first = found[0]
            where = ".".join(str(part) for part in first.get("loc") or ())
            message = str(first.get("msg") or "").removeprefix("Value error, ")
            return f"{where}: {message}" if where else message
    return str(error)


def render_skill_md(skill: SkillBundle) -> str:
    """:return: The skill's ``SKILL.md``: its frontmatter, then its instructions."""
    frontmatter: dict[str, Any] = {"name": skill.name, "description": skill.description}
    for key, value in (
        ("license", skill.license),
        ("compatibility", skill.compatibility),
        ("allowed-tools", skill.allowed_tools),
    ):
        if value:
            frontmatter[key] = value
    if skill.metadata:
        frontmatter["metadata"] = skill.metadata
    frontmatter.update(skill.extra)
    head = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True, width=1_000).strip()
    return f"---\n{head}\n---\n\n{skill.instructions.strip()}\n"


def ignored(path: str) -> bool:
    """:return: Whether a file is a folder's housekeeping (``.git``, ``.DS_Store``), never a skill's."""
    parts = path.split("/")
    return any(part in _IGNORED for part in parts) or parts[-1].startswith("._")


def find_skills(files: Mapping[str, bytes]) -> FoundSkills:
    """
    Read every skill in a folder: each folder with a ``SKILL.md`` is one,
    with the files under it (but not those of a skill inside it).

    :param files: The folder's files, by their paths in it.
    :return: The skills, the SKILL.md files that aren't skills as they are,
        and the files no skill has.
    """
    found = FoundSkills()
    cleaned: dict[str, bytes] = {}
    for path, data in files.items():
        try:
            clean = clean_path(path)
        except ValueError:
            found.skipped.append(path)
            continue
        if not ignored(clean):
            cleaned[clean] = data
    roots = sorted(
        {posixpath.dirname(path) for path in cleaned if posixpath.basename(path).lower() == "skill.md"},
        key=lambda root: (root.count("/"), root),
    )

    def owner(path: str) -> str | None:
        # The deepest skill folder it's in.
        best: str | None = None
        for root in roots:
            if (root == "" or path.startswith(f"{root}/")) and (best is None or len(root) > len(best)):
                best = root
        return best

    owned: dict[str, dict[str, bytes]] = {root: {} for root in roots}
    for path, data in cleaned.items():
        root = owner(path)
        if root is None:
            found.skipped.append(path)
            continue
        inner = path[len(root) + 1 :] if root else path
        if inner.lower() != "skill.md":
            owned[root][inner] = data

    for root in roots:
        where = f"{root}/SKILL.md" if root else "SKILL.md"
        text = next(
            cleaned[p]
            for p in cleaned
            if posixpath.dirname(p) == root and posixpath.basename(p).lower() == "skill.md"
        )
        try:
            skill_files = [SkillFile.of(path, data) for path, data in sorted(owned[root].items())]
            if len(skill_files) > MAX_FILES:
                raise SkillError(f"it has {len(skill_files)} files; a skill may have {MAX_FILES}")
            bundle = bundle_of(text.decode("utf-8"), skill_files, folder=root)
        except UnicodeDecodeError:
            found.problems.append(SkillProblem(where, "SKILL.md isn't UTF-8 text"))
            continue
        except (SkillError, ValueError) as error:
            found.problems.append(
                SkillProblem(where, _reason(error) if isinstance(error, ValueError) else str(error))
            )
            continue
        found.skills.append((root, bundle))
    return found


def unzip(data: bytes) -> dict[str, bytes]:
    """
    :return: A zip archive's files, by their paths in it (a ``.skill`` file is one too).
    :raises SkillError: It isn't a zip archive, or it unpacks to too much.
    """
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise SkillError("It isn't a zip archive") from None
    entries = [info for info in archive.infolist() if not info.is_dir()]
    if len(entries) > MAX_ARCHIVE_ENTRIES:
        raise SkillError(f"The archive has {len(entries)} files; it may have {MAX_ARCHIVE_ENTRIES}")
    if sum(info.file_size for info in entries) > MAX_ARCHIVE_BYTES:
        raise SkillError(f"The archive unpacks to more than {MAX_ARCHIVE_BYTES // 1_000_000} MB")
    files: dict[str, bytes] = {}
    total = 0
    for info in entries:
        with archive.open(info) as member:
            # Read no more than it says it holds: a forged size can't unpack more.
            content = member.read(info.file_size + 1)
        if len(content) > info.file_size:
            raise SkillError("The archive's sizes are wrong")
        total += len(content)
        if total > MAX_ARCHIVE_BYTES:
            raise SkillError(f"The archive unpacks to more than {MAX_ARCHIVE_BYTES // 1_000_000} MB")
        files[info.filename] = content
    return files


def adk_skill(bundle: SkillBundle) -> Skill:
    """
    :return: The skill as ADK reads one: its files under ``references/``,
        ``assets/`` and ``scripts/`` as they are; any other file a reference
        at its own path (``references/forms.md`` for ``forms.md``), which its
        instructions end by listing.
    """
    from google.adk.skills import Frontmatter, Resources, Script, Skill

    references: dict[str, str | bytes] = {}
    assets: dict[str, str | bytes] = {}
    scripts: dict[str, Script] = {}
    listed: list[str] = []
    for file in bundle.files:
        content: str | bytes = file.data if file.encoding == "base64" else file.content
        if file.path.startswith("scripts/") and isinstance(content, str):
            scripts[file.path.removeprefix("scripts/")] = Script(src=content)
            listed.append(f"- `{file.path}`")
        elif file.path.startswith("assets/") or file.path.startswith("scripts/"):
            assets[file.path.split("/", 1)[1]] = content
            listed.append(f"- `assets/{file.path.split('/', 1)[1]}`")
        elif file.path.startswith("references/"):
            references[file.path.removeprefix("references/")] = content
            listed.append(f"- `{file.path}`")
        else:
            references[file.path] = content
            listed.append(f"- `references/{file.path}` (the instructions call it `{file.path}`)")
    instructions = bundle.instructions
    if listed:
        instructions += (
            "\n\n---\nThis skill's files: read one with load_skill_resource, "
            f'skill_name="{bundle.name}" and its path here.\n' + "\n".join(listed)
        )
    frontmatter = Frontmatter.model_validate(
        {
            "name": bundle.name,
            "description": bundle.description,
            "license": bundle.license,
            "compatibility": bundle.compatibility,
            "allowed-tools": bundle.allowed_tools,
            "metadata": {
                key: value
                for key, value in bundle.metadata.items()
                # ADK reads these as its own: tools a skill turns on, state in
                # its instructions. Neither is anything Forge sets up.
                if key not in ("adk_additional_tools", "adk_inject_state")
            },
        }
    )
    return Skill(
        frontmatter=frontmatter,
        instructions=instructions,
        resources=Resources(references=references, assets=assets, scripts=scripts),
    )


def skill_toolset(
    bundles: Sequence[SkillBundle], *, code_executor: BaseCodeExecutor | None = None
) -> SkillToolset:
    """
    :param bundles: The skills, each named once.
    :param code_executor: What runs their scripts; without one, the model
        reads a script and follows it itself.
    :return: ADK's toolset over them: the model is told each skill's name and
        description up front, and loads one (``load_skill``) and its files
        (``load_skill_resource``) when a task calls for it.
    """
    from google.adk.tools.skill_toolset import SkillDiscoveryMode, SkillToolset

    return SkillToolset(
        [bundle.to_adk() for bundle in bundles],
        code_executor=code_executor,
        discovery_mode=SkillDiscoveryMode.EAGER,
    )


class SkillLibrary(Protocol):
    """Finds the organization's skills an agent names by ID (a ``skills`` node's ``skills``)."""

    async def __call__(self, skill_ids: Sequence[str], *, organization_id: str | None) -> list[SkillBundle]:
        """
        :return: The skills, in order.
        :raises LookupError: The organization has no such skill.
        """
        ...


class BundledSkills:
    """
    Skills carried with what runs (a workflow run, an exported agent), by
    ID, as their bundles' JSON.
    """

    def __init__(self, bundles: Mapping[str, Mapping[str, Any]]) -> None:
        self.bundles = dict(bundles)

    async def __call__(self, skill_ids: Sequence[str], *, organization_id: str | None) -> list[SkillBundle]:
        found = []
        for skill_id in skill_ids:
            raw = self.bundles.get(skill_id)
            if raw is None:
                raise LookupError(
                    f"there's no skill {skill_id} here: it was deleted, or isn't the organization's"
                )
            found.append(SkillBundle.model_validate(raw))
        return found
