"""
An organization's skills vault: skills (Agent Skills: a SKILL.md and the
files beside it) its agents' Skills tools and its workflows' LLM agents load
when a task calls for one.

A skill gets into the vault four ways, all ending in the same skill
(``forge_agent_runtime.skills.SkillBundle``):

- written in the web console (``POST …/skills``);
- uploaded: a folder of skills, one skill's folder, a single SKILL.md or a
  zip of any of them is read (``POST …/skills/read``), then the skills the
  person picks are added (``POST …/skills/import``);
- imported from GitHub: a repository's folder at a branch, tag or commit is
  read (``POST …/skills/github``), then added the same way; a skill from
  GitHub can be pulled again later (``POST …/skills/{id}/pull``);
- written by the skill creator, a helper agent that knows Forge and looks
  at what the organization has, in the background
  (``POST …/helpers/skill_creator``, ``forge_admin.skills.creator``): the
  skill it writes is added when it's done.

Reading needs ``organizations:read`` in the organization; everything else
``skills:manage`` (its admins and members have it by default).
"""

import io
import json
import logging
import posixpath
import zipfile
from typing import Annotated, Any

import httpx2 as httpx
from fastapi import (
    APIRouter,
    File,
    Form,
    HTTPException,
    Path,
    Request,
    Response,
    UploadFile,
    status,
)
from forge_agent_runtime import SkillBundle
from forge_agent_runtime.skills import (
    MAX_ARCHIVE_BYTES,
    FoundSkills,
    SkillError,
    find_skills,
    unzip,
)
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer

from forge_admin.api.routes.common import NodeId, Session, commit_or_conflict
from forge_admin.api_credentials import OrganizationCredentials
from forge_admin.auth.access import (
    UUID_PATTERN,
    CurrentUser,
    Enforcer,
    Level,
    Scope,
    authorize,
)
from forge_admin.db.audit import UtcDateTime
from forge_admin.models import ApiCredential, Skill
from forge_admin.skills.github import GitHubError, GitHubSkills, parse_location
from forge_admin.skills.vault import fill

logger = logging.getLogger(__name__)

router = APIRouter(tags=["organization skills"])

MANAGE = "skills:manage"
READ = "organizations:read"
NOT_FOUND = "The organization has no such skill"
NAME_TAKEN = "The organization already has a skill by that name"

SkillId = Annotated[str, Path(pattern=rf"^{UUID_PATTERN}$")]


class SkillSummary(BaseModel):
    """A skill as the vault lists it."""

    id: str
    organization_id: str
    name: str
    description: str
    file_count: int
    size_bytes: int
    #: Where it came from: ``{"kind": "written" | "upload" | "github" | "creator", ...}``.
    source: dict[str, Any]
    created_at: UtcDateTime
    created_by: str
    updated_at: UtcDateTime
    updated_by: str


class SkillRead(SkillSummary):
    """A skill whole: its SKILL.md's fields and its files."""

    skill: SkillBundle


class FoundSkill(BaseModel):
    """A skill found in an upload or a repository, not yet added."""

    #: Its folder in what was read (empty for the top).
    folder: str
    skill: SkillBundle
    #: Where it would come from, as the vault keeps it.
    source: dict[str, Any]
    #: The organization's skill of the same name, which adding it would replace.
    existing: str | None = None


class Problem(BaseModel):
    path: str
    message: str


class ReadResult(BaseModel):
    """What an upload or a repository holds."""

    skills: list[FoundSkill]
    #: SKILL.md files that aren't skills as they are, and why.
    problems: list[Problem]
    #: Files that aren't any skill's.
    skipped: list[str]


class GitHubRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: The repository's URL, a folder's or a SKILL.md's, or ``owner/repo``.
    url: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2_000)
    ]
    #: The folder its skills are in; the URL's, or the whole repository, when empty.
    path: str = Field(default="", max_length=1_000)
    #: The branch, tag or commit; the URL's, or the default branch, when empty.
    ref: str = Field(default="", max_length=255)
    #: One of the organization's credentials that signs in to GitHub, for a
    #: private repository; empty to read a public one.
    credential: str = Field(default="", max_length=36)


class ImportItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    skill: SkillBundle
    source: dict[str, Any] = Field(default_factory=dict)
    #: The organization's skill it replaces, by ID; None to add it.
    replace: str | None = Field(default=None, max_length=36)


class ImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    skills: list[ImportItem] = Field(min_length=1, max_length=100)


def _summary(row: Skill) -> SkillSummary:
    return SkillSummary.model_validate(
        {name: getattr(row, name) for name in SkillSummary.model_fields}
    )


def _read(row: Skill) -> SkillRead:
    return SkillRead.model_validate(
        {
            **{name: getattr(row, name) for name in SkillSummary.model_fields},
            "skill": row.bundle,
        }
    )


def _source(raw: dict[str, Any]) -> dict[str, Any]:
    """Where an imported skill came from, as the vault keeps it: what's known, as text."""
    kind = raw.get("kind")
    if kind == "github":
        keys = ("repository", "ref", "commit", "path", "credential")
        return {
            "kind": "github",
            **{k: str(raw[k])[:2_000] for k in keys if isinstance(raw.get(k), str)},
        }
    if kind == "upload":
        return {
            "kind": "upload",
            **({"folder": str(raw["folder"])[:1_000]} if raw.get("folder") else {}),
        }
    return {"kind": "written"}


async def _skill(
    session: AsyncSession, organization_id: str, skill_id: str, *, whole: bool = True
) -> Skill:
    query = select(Skill).where(
        Skill.id == skill_id, Skill.organization_id == organization_id
    )
    if whole:
        query = query.options(undefer(Skill.bundle))
    row = await session.scalar(query)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)
    return row


async def _existing(session: AsyncSession, organization_id: str) -> dict[str, str]:
    """:return: The organization's skills' IDs, by name."""
    rows = await session.execute(
        select(Skill.name, Skill.id).where(Skill.organization_id == organization_id)
    )
    return {name: skill_id for name, skill_id in rows}


async def _result(
    session: AsyncSession,
    organization_id: str,
    found: FoundSkills,
    source: Any,
) -> ReadResult:
    existing = await _existing(session, organization_id)
    return ReadResult(
        skills=[
            FoundSkill(
                folder=folder,
                skill=skill,
                source=source(folder),
                existing=existing.get(skill.name),
            )
            for folder, skill in found.skills
        ],
        problems=[Problem(path=p.path, message=p.message) for p in found.problems],
        skipped=found.skipped[:200],
    )


# --------------------------------------------------------------------------
# The vault
# --------------------------------------------------------------------------


@router.get("/organizations/{organization_id}/skills")
async def list_skills(
    organization_id: NodeId,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> list[SkillSummary]:
    """
    List the organization's skills, by name.
    \f
    :raises HTTPException: 403 without organizations:read in the organization.
    """
    await authorize(session, enforcer, user, READ, Scope(Level.ORG, organization_id))
    rows = await session.scalars(
        select(Skill)
        .where(Skill.organization_id == organization_id)
        .order_by(Skill.name)
    )
    return [_summary(row) for row in rows]


@router.post(
    "/organizations/{organization_id}/skills", status_code=status.HTTP_201_CREATED
)
async def create_skill(
    organization_id: NodeId,
    body: SkillBundle,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> SkillRead:
    """
    Add a skill written in the web console.
    \f
    :raises HTTPException: 403 without skills:manage; 409 when the name is
        taken; 422 when it isn't a skill as it is.
    """
    await authorize(session, enforcer, user, MANAGE, Scope(Level.ORG, organization_id))
    row = Skill(organization_id=organization_id, source={"kind": "written"})
    fill(row, body)
    session.add(row)
    await commit_or_conflict(session, NAME_TAKEN)
    return _read(row)


@router.get("/organizations/{organization_id}/skills/{skill_id}")
async def get_skill(
    organization_id: NodeId,
    skill_id: SkillId,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> SkillRead:
    """
    One of the organization's skills, whole.
    \f
    :raises HTTPException: 403 without organizations:read; 404 when it has
        no such skill.
    """
    await authorize(session, enforcer, user, READ, Scope(Level.ORG, organization_id))
    return _read(await _skill(session, organization_id, skill_id))


@router.put("/organizations/{organization_id}/skills/{skill_id}")
async def update_skill(
    organization_id: NodeId,
    skill_id: SkillId,
    body: SkillBundle,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> SkillRead:
    """
    Change a skill: it's replaced whole. Agents using it read it as it is
    now from their next conversation, and workflows from their next run.
    \f
    :raises HTTPException: 403 without skills:manage; 404 when the
        organization has no such skill; 409 when the name is taken; 422 when
        it isn't a skill as it is.
    """
    await authorize(session, enforcer, user, MANAGE, Scope(Level.ORG, organization_id))
    row = await _skill(session, organization_id, skill_id)
    fill(row, body)
    await commit_or_conflict(session, NAME_TAKEN)
    return _read(row)


@router.delete(
    "/organizations/{organization_id}/skills/{skill_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_skill(
    organization_id: NodeId,
    skill_id: SkillId,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> Response:
    """
    Delete a skill. Agents and workflows naming it fail to build until it's
    taken off them.
    \f
    :raises HTTPException: 403 without skills:manage; 404 when the
        organization has no such skill.
    """
    await authorize(session, enforcer, user, MANAGE, Scope(Level.ORG, organization_id))
    row = await _skill(session, organization_id, skill_id, whole=False)
    await session.delete(row)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/organizations/{organization_id}/skills/{skill_id}/download")
async def download_skill(
    organization_id: NodeId,
    skill_id: SkillId,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> Response:
    """
    A skill as a zip of its folder: ``<name>/SKILL.md`` and its files, as
    other tools that read skills take one.
    \f
    :raises HTTPException: 403 without organizations:read; 404 when it has
        no such skill.
    """
    await authorize(session, enforcer, user, READ, Scope(Level.ORG, organization_id))
    row = await _skill(session, organization_id, skill_id)
    skill = SkillBundle.model_validate(row.bundle)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(f"{skill.name}/SKILL.md", skill.skill_md())
        for file in skill.files:
            archive.writestr(f"{skill.name}/{file.path}", file.data)
    return Response(
        buffer.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{skill.name}.zip"'},
    )


# --------------------------------------------------------------------------
# Uploads and GitHub
# --------------------------------------------------------------------------


@router.post("/organizations/{organization_id}/skills/read")
async def read_upload(
    organization_id: NodeId,
    files: Annotated[list[UploadFile], File()],
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
    paths: Annotated[str | None, Form(max_length=1_000_000)] = None,
) -> ReadResult:
    """
    Read the skills in an upload, without adding them: a folder of skills
    (each a folder with a SKILL.md), one skill's folder, a single SKILL.md
    (named anything ending in .md), or a zip of any of these (a ``.skill``
    file is one).
    \f
    :param files: The files.
    :param paths: Each file's path in the folder uploaded, in order, as a
        JSON list: browsers send only a file's name. A file without one is
        at its name.
    :raises HTTPException: 403 without skills:manage; 413 when the upload
        is larger than 50 MB; 422 for an archive that isn't one, or paths
        that don't match the files.
    """
    await authorize(session, enforcer, user, MANAGE, Scope(Level.ORG, organization_id))
    try:
        named = json.loads(paths) if paths else []
    except ValueError:
        named = None
    if not isinstance(named, list) or not all(isinstance(p, str) for p in named):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "paths is a JSON list of the files' paths",
        )
    contents: dict[str, bytes] = {}
    total = 0
    for index, upload in enumerate(files):
        data = await upload.read(MAX_ARCHIVE_BYTES - total + 1)
        total += len(data)
        if total > MAX_ARCHIVE_BYTES:
            raise HTTPException(
                status.HTTP_413_CONTENT_TOO_LARGE,
                f"An upload of skills is {MAX_ARCHIVE_BYTES // 1_000_000} MB at most",
            )
        name = named[index] if index < len(named) else (upload.filename or "")
        contents[name.replace("\\", "/").lstrip("/") or f"file-{index}"] = data
    if len(contents) == 1:
        [(only, data)] = contents.items()
        lower = only.lower()
        if lower.endswith((".zip", ".skill")) or data[:4] == b"PK\x03\x04":
            try:
                contents = unzip(data)
            except SkillError as error:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)
                ) from None
        elif lower.endswith(".md") and posixpath.basename(lower) != "skill.md":
            # A single SKILL.md, named for its skill (pdf-forms.md).
            folder = posixpath.splitext(posixpath.basename(only))[0]
            contents = {f"{folder}/SKILL.md": data}
    found = find_skills(contents)
    return await _result(
        session,
        organization_id,
        found,
        lambda folder: {"kind": "upload", **({"folder": folder} if folder else {})},
    )


def github_client(request: Request) -> httpx.AsyncClient:
    """What GitHub is read with: a test's transport when it set one."""
    transport = getattr(request.app.state, "github_transport", None)
    return httpx.AsyncClient(transport=transport) if transport else httpx.AsyncClient()


async def _github(
    request: Request,
    session: AsyncSession,
    organization_id: str,
    body: GitHubRead,
) -> tuple[Any, Any]:
    """
    :return: What the folder holds, read with the credential when there's one.
    :raises HTTPException: The repository can't be read, or the credential
        isn't the organization's or can't be sent to GitHub.
    """
    try:
        location = parse_location(body.url, path=body.path, ref=body.ref)
    except GitHubError as error:
        raise HTTPException(error.status, str(error)) from None
    sign = None
    if body.credential:
        credential = await session.get(ApiCredential, body.credential)
        if credential is None or credential.organization_id != organization_id:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "The organization has no such credential",
            )
        credentials = getattr(request.app.state, "api_credentials", None)
        if credentials is None:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "Credentials aren't set up: set FORGE_ADMIN_SECRETS_KEY",
            )
        provider = OrganizationCredentials(credentials, request.app.state.sessionmaker)

        async def sign(url: str) -> dict[str, str]:
            try:
                auth = await provider(
                    body.credential, organization_id=organization_id, url=url
                )
            except LookupError as error:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_CONTENT,
                    f"The credential can't be used for GitHub: {error}",
                ) from None
            return dict(auth.headers)

    async with github_client(request) as client:
        try:
            imported = await GitHubSkills(client, sign).read(location)
        except GitHubError as error:
            raise HTTPException(error.status, str(error)) from None
    return imported, location


@router.post("/organizations/{organization_id}/skills/github")
async def read_github(
    organization_id: NodeId,
    body: GitHubRead,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> ReadResult:
    """
    Read the skills in a GitHub repository's folder, without adding them:
    every folder with a SKILL.md in it, at the ref's commit.
    \f
    :raises HTTPException: 403 without skills:manage; 404 when the
        repository, ref or folder isn't there (or can't be seen without a
        credential); 422 for a URL that isn't GitHub's, a folder too large to
        import, or a credential that can't be used; 429 when GitHub's rate
        limit is used up; 502 when GitHub fails.
    """
    await authorize(session, enforcer, user, MANAGE, Scope(Level.ORG, organization_id))
    imported, _ = await _github(request, session, organization_id, body)
    return await _result(
        session,
        organization_id,
        imported.found,
        lambda folder: imported.source(folder, body.credential or None),
    )


@router.post("/organizations/{organization_id}/skills/import")
async def import_skills(
    organization_id: NodeId,
    body: ImportRequest,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> list[SkillSummary]:
    """
    Add skills read from an upload or GitHub, together: each new, or in
    place of one of the organization's (``replace``). Nothing is added if
    one can't be.
    \f
    :raises HTTPException: 403 without skills:manage; 404 when a skill to
        replace isn't the organization's; 409 when a name is taken by a skill
        not being replaced, or given twice.
    """
    await authorize(session, enforcer, user, MANAGE, Scope(Level.ORG, organization_id))
    names = [item.skill.name for item in body.skills]
    if len(set(names)) != len(names):
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Two of the skills have the same name"
        )
    replacing = [item.replace for item in body.skills if item.replace]
    if len(set(replacing)) != len(replacing):
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Two of the skills replace the same one"
        )
    existing = await _existing(session, organization_id)
    replaced = set(replacing)
    rows: list[Skill] = []
    for item in body.skills:
        taken = existing.get(item.skill.name)
        if taken is not None and taken != item.replace and taken not in replaced:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"The organization already has a skill called {item.skill.name}: "
                "replace it, or rename this one",
            )
        if item.replace:
            row = await _skill(session, organization_id, item.replace)
        else:
            row = Skill(organization_id=organization_id)
            session.add(row)
        fill(row, item.skill)
        row.source = _source(item.source)
        rows.append(row)
    await commit_or_conflict(session, NAME_TAKEN)
    return [_summary(row) for row in rows]


@router.post("/organizations/{organization_id}/skills/{skill_id}/pull")
async def pull_skill(
    organization_id: NodeId,
    skill_id: SkillId,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> SkillRead:
    """
    Pull a skill imported from GitHub again: its folder at the branch or tag
    it came from, as it is now (with the credential it was read with).
    Changes made here since are replaced.
    \f
    :raises HTTPException: 403 without skills:manage; 404 when there's no
        such skill, or its folder holds no skill any more; 409 when it
        didn't come from GitHub; others as reading GitHub does.
    """
    await authorize(session, enforcer, user, MANAGE, Scope(Level.ORG, organization_id))
    row = await _skill(session, organization_id, skill_id)
    source = dict(row.source or {})
    if source.get("kind") != "github" or not source.get("repository"):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "It didn't come from GitHub: there's nothing to pull",
        )
    body = GitHubRead(
        url=str(source["repository"]),
        path=str(source.get("path") or ""),
        ref=str(source.get("ref") or ""),
        credential=str(source.get("credential") or ""),
    )
    imported, _ = await _github(request, session, organization_id, body)
    found = next(
        (skill for folder, skill in imported.found.skills if folder == body.path),
        None,
    )
    if found is None:
        problem = next(iter(imported.found.problems), None)
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"{body.path or 'The repository'} at {imported.ref} holds no skill now"
            + (f": {problem.message}" if problem else ""),
        )
    fill(row, found)
    row.source = imported.source(body.path, body.credential or None)
    await commit_or_conflict(session, NAME_TAKEN)
    return _read(row)
