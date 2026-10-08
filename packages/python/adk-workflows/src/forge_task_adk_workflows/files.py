"""
Files a run starts with. A workflow's start takes them when it allows files
(``allow_files``), of the types it names (``file_types``, ids of
:data:`FILE_TYPES`; none names any type). Whoever starts the run sends them
with its input, and the admin API checks each against the start
(:func:`checked_file`) and saves it as an ADK artifact of the run's session
(:func:`save_run_files`: app ``adk_workflows``, the user the run acts as,
the run's session ID) before the run is queued.

The worker's runner has the same artifact service, so a node reads a file as
ADK has it read artifacts: ``ctx.load_artifact(name)``, and an LLM agent
with ADK's ``load_artifacts`` tool, which the agents of a run with files
are given. The run's ``state.files`` lists them (:class:`RunFile`), and its
payload keeps the list. A run that starts over in a new session takes its
files along (:func:`copy_run_files`).

The artifacts are where :func:`artifact_service` says: ``s3://bucket/prefix``
(``forge_common.adk.artifacts.S3ArtifactService``, which the admin API and
every worker can reach), a folder they share, or ``memory`` (one process
only: tests).

The web builder's own list of the types is
``apps/forge-web/src/features/adk-workflows/lib/files.ts``: keep the two
alike (the admin API's tests compare this one with the format's schema).
"""

from __future__ import annotations

import mimetypes
import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

from google.adk.artifacts import BaseArtifactService
from google.genai import types
from pydantic import BaseModel, Field

__all__ = [
    "FILE_TYPES",
    "FileRefused",
    "FileType",
    "FilesRule",
    "RunFile",
    "artifact_service",
    "checked_file",
    "clean_name",
    "copy_run_files",
    "files_rule",
    "save_run_files",
    "unique_names",
]

#: The longest name a file keeps.
MAX_NAME = 200
#: ADK reads an artifact named so as the user's, every session's: a run's
#: files are never.
_USER_SCOPE = "user:"
_MEDIA_TYPE = re.compile(r"^[A-Za-z0-9][\w.+-]*/[A-Za-z0-9][\w.+-]*$")


@dataclass(frozen=True)
class FileType:
    """
    A type of file a start can take.

    :ivar id: How a start names it (``file_types``).
    :ivar extensions: Its file name extensions, each with its media type.
    """

    id: str
    extensions: Mapping[str, str]


def _type(type_id: str, **extensions: str) -> FileType:
    return FileType(type_id, {f".{ext}": media for ext, media in extensions.items()})


#: The types a start can name, by ID.
FILE_TYPES: dict[str, FileType] = {
    t.id: t
    for t in (
        _type("pdf", pdf="application/pdf"),
        _type(
            "word",
            docx="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            doc="application/msword",
        ),
        _type(
            "powerpoint",
            pptx="application/vnd.openxmlformats-officedocument.presentationml.presentation",
            ppt="application/vnd.ms-powerpoint",
        ),
        _type("text", txt="text/plain"),
        _type("markdown", md="text/markdown", markdown="text/markdown"),
        _type("html", html="text/html", htm="text/html"),
        _type(
            "excel",
            xlsx="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            xls="application/vnd.ms-excel",
        ),
        _type("csv", csv="text/csv"),
        _type("json", json="application/json"),
        _type("xml", xml="application/xml"),
        _type("png", png="image/png"),
        _type("jpeg", jpg="image/jpeg", jpeg="image/jpeg"),
        _type("gif", gif="image/gif"),
        _type("webp", webp="image/webp"),
        _type("mp3", mp3="audio/mpeg"),
        _type("wav", wav="audio/wav"),
        _type("mp4", mp4="video/mp4"),
        _type("zip", zip="application/zip"),
    )
}
_BY_EXTENSION = {ext: (t, media) for t in FILE_TYPES.values() for ext, media in t.extensions.items()}


class RunFile(BaseModel):
    """A file a run started with: an artifact of its session, as ``state.files`` lists it."""

    #: The artifact's name: ``ctx.load_artifact(name)``.
    name: str = Field(min_length=1)
    media_type: str
    size_bytes: int = Field(ge=0)
    #: Its type (an ID of ``FILE_TYPES``); empty when it's none of them.
    type: str = ""
    #: The artifact's version: 0, its first.
    version: int = Field(default=0, ge=0)


@dataclass(frozen=True)
class FilesRule:
    """
    What files a start takes.

    :ivar allowed: Whether it takes any.
    :ivar types: The types it takes, by ID; none takes any type.
    """

    allowed: bool = False
    types: tuple[str, ...] = ()

    def describe(self) -> str:
        """:return: The types it takes, for a refusal: ``pdf, word``, or ``any type``."""
        return ", ".join(self.types) if self.types else "any type"


def files_rule(document: Mapping[str, Any]) -> FilesRule:
    """:return: What files a workflow's start takes; none when it has no start."""
    for node in document.get("nodes") or []:
        if isinstance(node, dict) and node.get("kind") == "start":
            config = node.get("config")
            config = config if isinstance(config, dict) else {}
            listed = config.get("file_types")
            types = tuple(t for t in listed if isinstance(t, str)) if isinstance(listed, list) else ()
            return FilesRule(allowed=config.get("allow_files") is True, types=types)
    return FilesRule()


class FileRefused(ValueError):
    """A file a run can't take: the message says why."""


def clean_name(name: str | None) -> str:
    """
    :param name: A file's name as it was sent, perhaps with a path.
    :return: The name it's kept under: its last part, without control
        characters or ADK's ``user:`` scope, at most :data:`MAX_NAME`
        characters (its extension kept); ``file`` when nothing is left.
    """
    text = unicodedata.normalize("NFC", name or "")
    text = PureWindowsPath(PurePosixPath(text).name).name
    text = "".join(c for c in text if unicodedata.category(c)[0] != "C").strip()
    while text.lower().startswith(_USER_SCOPE):
        text = text[len(_USER_SCOPE) :].strip()
    text = text.strip(". ") or "file"
    if len(text) > MAX_NAME:
        stem, dot, ext = text.rpartition(".")
        keep = f".{ext}" if dot and stem and len(ext) <= 16 else ""
        text = text[: MAX_NAME - len(keep)].rstrip() + keep
    return text


def unique_names(names: Iterable[str]) -> list[str]:
    """:return: The names in order, a repeated one numbered: ``a.pdf``, ``a (2).pdf``."""
    taken: set[str] = set()
    unique: list[str] = []
    for name in names:
        stem, dot, ext = name.rpartition(".")
        stem, ext = (stem, f".{ext}") if dot and stem else (name, "")
        candidate, n = name, 1
        while candidate.casefold() in taken:
            n += 1
            candidate = f"{stem} ({n}){ext}"
        taken.add(candidate.casefold())
        unique.append(candidate)
    return unique


def _extension(name: str) -> str:
    _, dot, ext = name.rpartition(".")
    return f".{ext.lower()}" if dot else ""


def checked_file(rule: FilesRule, name: str | None, media_type: str | None) -> tuple[str, str, str]:
    """
    A file a run is started with, held to its start.

    :param rule: What the start takes.
    :param name: The file's name, as it was sent.
    :param media_type: The media type it was sent with, if any.
    :return: The name it's kept under (:func:`clean_name`), its type's ID
        (empty when it's none of :data:`FILE_TYPES`), and its media type: its
        type's, by its extension, else the one it was sent with, else a
        guess from its name.
    :raises FileRefused: The start takes no files, or not of its type.
    """
    kept = clean_name(name)
    if not rule.allowed:
        raise FileRefused("The workflow's start doesn't take files")
    found = _BY_EXTENSION.get(_extension(kept))
    if rule.types and (found is None or found[0].id not in rule.types):
        raise FileRefused(f"{kept}: the workflow's start takes {rule.describe()} files only")
    if found is not None:
        return kept, found[0].id, found[1]
    sent = (media_type or "").split(";", 1)[0].strip().lower()
    if not _MEDIA_TYPE.match(sent) or sent == "application/octet-stream":
        sent = mimetypes.guess_type(kept, strict=False)[0] or "application/octet-stream"
    return kept, "", sent


async def save_run_files(
    artifacts: BaseArtifactService,
    *,
    app_name: str,
    user_id: str,
    session_id: str,
    files: Iterable[tuple[str, str, str, bytes]],
) -> list[RunFile]:
    """
    Save the files a run starts with, as artifacts of its session.

    :param artifacts: Where the run's artifacts are.
    :param app_name: The runs' ADK app.
    :param user_id: Who the run acts as: its session's user.
    :param session_id: The run's session.
    :param files: Each one's name (:func:`unique_names` of
        :func:`checked_file`'s), type, media type and bytes.
    :return: The files, as the run lists them.
    """
    saved: list[RunFile] = []
    for name, type_id, media_type, data in files:
        version = await artifacts.save_artifact(
            app_name=app_name,
            user_id=user_id,
            session_id=session_id,
            filename=name,
            artifact=types.Part(inline_data=types.Blob(data=data, mime_type=media_type, display_name=name)),
        )
        saved.append(RunFile(name=name, media_type=media_type, size_bytes=len(data), type=type_id, version=version))
    return saved


async def copy_run_files(
    artifacts: BaseArtifactService,
    *,
    app_name: str,
    user_id: str,
    from_session: str,
    to_session: str,
    files: Iterable[RunFile],
) -> None:
    """
    Take a run's files to the new session it starts over in. One already
    there is left as it is (an earlier attempt copied it).

    :raises FileNotFoundError: One isn't where the run started any more.
    """
    for file in files:
        there = await artifacts.load_artifact(
            app_name=app_name, user_id=user_id, session_id=to_session, filename=file.name
        )
        if there is not None:
            continue
        found = await artifacts.load_artifact(
            app_name=app_name,
            user_id=user_id,
            session_id=from_session,
            filename=file.name,
            version=file.version,
        )
        if found is None:
            raise FileNotFoundError(f"The run's file {file.name} isn't kept any more")
        await artifacts.save_artifact(
            app_name=app_name, user_id=user_id, session_id=to_session, filename=file.name, artifact=found
        )


def artifact_service(
    where: str,
    *,
    endpoint_url: str | None = None,
    region: str | None = None,
    access_key_id: str | None = None,
    secret_access_key: str | None = None,
    create_bucket: bool = False,
) -> BaseArtifactService:
    """
    Where runs' artifacts are kept.

    :param where: ``s3://bucket/prefix`` (S3, or anything that speaks it, at
        ``endpoint_url``), a folder (``file:///path`` or a path), or
        ``memory`` (this process only: tests).
    :param endpoint_url: S3 that isn't Amazon's (the local RustFS); None for AWS.
    :param region: S3's region.
    :param access_key_id: S3's credentials; boto3's own lookup without them.
    :param secret_access_key: S3's credentials.
    :param create_bucket: Make the bucket on first use when it isn't there
        (local development).
    :raises ValueError: ``where`` is none of them.
    """
    where = where.strip()
    if where == "memory":
        from google.adk.artifacts import InMemoryArtifactService

        return InMemoryArtifactService()
    if where.startswith("s3://"):
        from forge_common.adk.artifacts import S3ArtifactService

        environment = {
            "AWS_ACCESS_KEY_ID": access_key_id or "",
            "AWS_SECRET_ACCESS_KEY": secret_access_key or "",
            "AWS_REGION": region or "",
            "AWS_ENDPOINT_URL_S3": endpoint_url or "",
        }
        return S3ArtifactService.from_url(where, environment, create_bucket=create_bucket)
    scheme, sep, _ = where.partition("://")
    if sep and scheme != "file":
        raise ValueError(f"Artifacts are kept in s3://..., a folder or memory, not {scheme}://")
    if not where:
        raise ValueError("Say where artifacts are kept: s3://bucket/prefix, a folder or memory")
    from google.adk.artifacts import FileArtifactService

    return FileArtifactService(root_dir=where.removeprefix("file://"))
