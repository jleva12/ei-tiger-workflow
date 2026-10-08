"""
The files a workflow run starts with, as its callers send them: a run's
start takes them when it allows files, of the types it names
(``forge_task_adk_workflows.files``). Starting a run (the console's
``POST …/agents/{id}/runs``, the runtime's ``POST /workflows/{ref}/runs``)
takes its body either way:

- ``multipart/form-data``: ``input`` as JSON text, the body's other fields
  (``version``, ``wait``) as text, and each file as a ``files`` part;
- ``application/json``: the body, its ``files`` each
  ``{"name", "content": <base64>, "media_type"}``.

A2A sends them as a message's file parts, with their bytes (``a2a.py``).

Each file is held to the start (its type, by its name's extension) and the
limits (``workflow_files_max_count`` files of at most
``workflow_files_max_bytes`` each), then saved as an ADK artifact of the
run's session, where the worker's runs keep theirs
(``workflow_artifacts``), before the run is queued (``runs.start_adk_run``).
"""

import base64
import binascii
import json
import logging
import re
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Self
from urllib.parse import quote

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from forge_task_adk_workflows.files import (
    FileRefused,
    RunFile,
    artifact_service,
    checked_file,
    files_rule,
    unique_names,
)
from google.adk.artifacts import BaseArtifactService
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.datastructures import UploadFile

from forge_admin.config import Settings

logger = logging.getLogger(__name__)

#: The form fields files come in.
FILE_FIELDS = ("files", "file")
NOT_SET_UP = (
    "Workflow runs can't take files here: set FORGE_ADMIN_WORKFLOW_ARTIFACTS "
    "(and the async worker's HYBRID_ADK_WORKFLOWS__ARTIFACTS) to where they're kept"
)
STORAGE_DOWN = "The workflow files' storage isn't answering; try again shortly"


class FileContent(BaseModel):
    """A file sent in a JSON body."""

    model_config = ConfigDict(extra="forbid")

    #: Its name, with its extension: the start takes files by their type.
    name: str = Field(min_length=1, max_length=1024)
    #: Its bytes, base64.
    content: str
    #: Its media type; its type's by its extension when the start names that.
    media_type: str | None = Field(default=None, max_length=255)


@dataclass(frozen=True)
class SentFile:
    """A file as it was sent: its name and media type, if any, and bytes."""

    name: str | None
    media_type: str | None
    data: bytes


@dataclass(frozen=True)
class CheckedFile:
    """A file the run's start takes: its name (unique among the run's), type
    (``FILE_TYPES``' ID, or empty), media type and bytes."""

    name: str
    type: str
    media_type: str
    data: bytes

    def saved(self) -> tuple[str, str, str, bytes]:
        return self.name, self.type, self.media_type, self.data


class FilesRefused(ValueError):
    """The files don't fit the run's start: the message says why."""


class Limits:
    """How many files a run may start with, and how big each may be."""

    def __init__(self, count: int, size: int) -> None:
        self.count = count
        self.size = size

    @classmethod
    def of(cls, settings: Settings) -> Self:
        return cls(settings.workflow_files_max_count, settings.workflow_files_max_bytes)

    def check_count(self, count: int) -> None:
        """:raises HTTPException: 422 for more files than a run may start with."""
        if count > self.count:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                f"A run starts with at most {self.count} files; {count} were sent",
            )

    def check_size(self, name: str | None, size: int) -> None:
        """:raises HTTPException: 413 for a file bigger than one may be."""
        if size > self.size:
            raise HTTPException(
                status.HTTP_413_CONTENT_TOO_LARGE,
                f"{name or 'A file'} is bigger than a run's files may be ({self.size:,} bytes)",
            )

    def checked(self, files: list[SentFile]) -> list[SentFile]:
        """:return: The files, once there aren't too many or too big ones."""
        self.check_count(len(files))
        for file in files:
            self.check_size(file.name, len(file.data))
        return files


def checked_files(
    document: dict[str, Any], files: Iterable[SentFile]
) -> list[CheckedFile]:
    """
    The files a run of the document is started with, held to its start.

    :return: Each, under a name of its own.
    :raises FilesRefused: The start takes no files, or not one's type, or one is empty.
    """
    rule = files_rule(document)
    found: list[tuple[str, str, str, bytes]] = []
    for file in files:
        try:
            name, type_id, media_type = checked_file(rule, file.name, file.media_type)
        except FileRefused as error:
            raise FilesRefused(str(error)) from None
        if not file.data:
            raise FilesRefused(f"{name} is empty")
        found.append((name, type_id, media_type, file.data))
    names = unique_names(name for name, *_ in found)
    return [
        CheckedFile(name, t, media, data)
        for name, (_, t, media, data) in zip(names, found, strict=True)
    ]


def decoded(files: Iterable[FileContent], limits: Limits) -> list[SentFile]:
    """
    The files of a JSON body.

    :raises HTTPException: 413 for one too big; 422 for too many, or content
        that isn't base64.
    """
    listed = list(files)
    limits.check_count(len(listed))
    sent: list[SentFile] = []
    for file in listed:
        # Four characters of base64 carry three bytes: say it's too big
        # before decoding all of it.
        limits.check_size(file.name, (len(file.content) * 3) // 4 - 2)
        try:
            data = base64.b64decode(file.content, validate=True)
        except (binascii.Error, ValueError):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                f"{file.name}: its content isn't base64",
            ) from None
        limits.check_size(file.name, len(data))
        sent.append(SentFile(file.name, file.media_type, data))
    return sent


async def read_run_body[M: BaseModel](
    request: Request, model: type[M], limits: Limits
) -> tuple[M, list[SentFile]]:
    """
    A request starting a run: its body, as JSON or a multipart form, and the
    files it sends.

    :param request: The request.
    :param model: The body, as JSON sends it; its ``files`` are the JSON's.
    :param limits: How many files, and how big.
    :return: The body (``files`` emptied) and the files.
    :raises RequestValidationError: The body isn't the model's (422).
    :raises HTTPException: 413 for a file too big, 422 for too many files or
        ``input`` that isn't JSON, 415 for another content type.
    """
    media = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    # A form without files comes url-encoded.
    if media in ("multipart/form-data", "application/x-www-form-urlencoded"):
        raw: dict[str, Any] = {}
        sent: list[SentFile] = []
        async with request.form(max_files=limits.count) as form:
            for key, value in form.multi_items():
                if isinstance(value, UploadFile):
                    if key not in FILE_FIELDS:
                        raise _invalid(
                            key, "Files go in the files field", value.filename
                        )
                    data = await value.read(limits.size + 1)
                    limits.check_size(value.filename, len(data))
                    sent.append(SentFile(value.filename, value.content_type, data))
                elif key == "input":
                    try:
                        raw["input"] = json.loads(value) if value.strip() else None
                    except ValueError:
                        raise _invalid(
                            "input",
                            "It isn't JSON: send the input as JSON text",
                            value[:200],
                        ) from None
                else:
                    raw[key] = value
        body = _validated(model, raw)
        limits.check_count(len(sent))
        return body, sent
    if media not in ("", "application/json") and not media.endswith("+json"):
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            "Send the run as application/json, or as multipart/form-data with its files",
        )
    content = await request.body()
    try:
        raw_json = json.loads(content) if content.strip() else {}
    except ValueError as error:
        where = error.pos if isinstance(error, json.JSONDecodeError) else 0
        raise RequestValidationError(
            [
                {
                    "type": "json_invalid",
                    "loc": ("body", where),
                    "msg": "JSON decode error",
                    "input": {},
                }
            ]
        ) from None
    body = _validated(model, raw_json)
    files = getattr(body, "files", None) or []
    sent = decoded(files, limits)
    if files:
        body = body.model_copy(update={"files": []})
    return body, sent


def _validated[M: BaseModel](model: type[M], raw: Any) -> M:
    try:
        return model.model_validate(raw)
    except ValidationError as error:
        raise RequestValidationError(
            [{**e, "loc": ("body", *e["loc"])} for e in error.errors(include_url=False)]
        ) from None


def _invalid(field: str, message: str, value: Any) -> RequestValidationError:
    return RequestValidationError(
        [
            {
                "type": "value_error",
                "loc": ("body", field),
                "msg": message,
                "input": value,
            }
        ]
    )


def run_body(model: type[BaseModel], description: str) -> dict[str, Any]:
    """
    How a run's start is sent, for the route's OpenAPI: the model as JSON,
    or as a multipart form with its files.

    :param model: The body, as JSON sends it.
    :param description: What the body is.
    :return: The route's ``openapi_extra``.
    """
    schema = _inlined(model.model_json_schema())
    fields = {
        name: (
            {"type": "string", "description": "The input, as JSON text"}
            if name == "input"
            else spec
        )
        for name, spec in schema.get("properties", {}).items()
        if name != "files"
    }
    fields["files"] = {
        "type": "array",
        "items": {"type": "string", "format": "binary"},
        "description": "The files the run starts with, when its start takes files",
    }
    return {
        "requestBody": {
            "required": True,
            "description": description,
            "content": {
                "application/json": {"schema": schema},
                "multipart/form-data": {
                    "schema": {"type": "object", "properties": fields}
                },
            },
        }
    }


def _inlined(schema: dict[str, Any]) -> dict[str, Any]:
    """:return: A JSON Schema with its ``$defs`` put where they're referred to."""
    defs = schema.pop("$defs", {})

    def walk(value: Any) -> Any:
        if isinstance(value, dict):
            ref = value.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                return walk(defs[ref.removeprefix("#/$defs/")])
            return {key: walk(item) for key, item in value.items()}
        if isinstance(value, list):
            return [walk(item) for item in value]
        return value

    inlined: dict[str, Any] = walk(schema)
    return inlined


def workflow_artifacts(settings: Settings) -> BaseArtifactService | None:
    """
    :return: Where workflow runs' files are saved (``workflow_artifacts``, on
        the S3 service of the ``s3_*`` settings); None when not set up.
    :raises ValueError: ``workflow_artifacts`` names no place they can be kept.
    """
    if settings.workflow_artifacts is None:
        return None
    secret = settings.s3_secret_access_key
    return artifact_service(
        settings.workflow_artifacts,
        endpoint_url=settings.s3_endpoint_url,
        region=settings.s3_region,
        access_key_id=settings.s3_access_key_id,
        secret_access_key=secret.get_secret_value() if secret else None,
        create_bucket=settings.s3_create_bucket,
    )


def artifacts_of(request: Request, files: list[SentFile]) -> BaseArtifactService | None:
    """
    :return: Where the run's files are saved: the app's artifact service;
        None when there are no files.
    :raises HTTPException: 503 when files were sent and runs can't take them here.
    """
    if not files:
        return None
    artifacts: BaseArtifactService | None = getattr(
        request.app.state, "workflow_artifacts", None
    )
    if artifacts is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, NOT_SET_UP)
    return artifacts


@contextmanager
def storage_errors() -> Iterator[None]:
    """:raises HTTPException: 503 when the artifacts' storage isn't answering."""
    try:
        yield
    except (BotoCoreError, ClientError, OSError) as error:
        logger.warning("The workflow files' storage failed: %s", error)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, STORAGE_DOWN) from None


def run_files(payload: Any) -> list[RunFile]:
    """:return: The files a run started with, as its payload keeps them."""
    listed = payload.get("files") if isinstance(payload, dict) else None
    found: list[RunFile] = []
    for item in listed if isinstance(listed, list) else []:
        try:
            found.append(RunFile.model_validate(item))
        except ValidationError:
            continue
    return found


def attachment(name: str) -> str:
    """:return: A Content-Disposition that downloads a file under its name, in any language."""
    # An ASCII name for old clients, and the real one (RFC 6266 / 5987).
    ascii_name = re.sub(r'[^\x20-\x7e]|["\\]', "_", name)
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name)}"
