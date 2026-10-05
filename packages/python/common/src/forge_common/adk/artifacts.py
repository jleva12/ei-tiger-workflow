"""ADK artifacts in S3, or anything that speaks it (MinIO, RustFS, Cloudflare R2).
Needs ``forge-common[adk-artifacts-s3]``.

:class:`S3ArtifactService` is ADK's ``GcsArtifactService`` on S3: the same keys
(under an optional prefix), one object per version, and the same rules for
what's session-scoped and what's the user's::

    {prefix}{app}/{user}/{session}/{filename}/{version}    a session's files
    {prefix}{app}/{user}/user/{filename}/{version}         "user:..." files, every session's

A save takes the next version with a conditional write (``If-None-Match: *``),
so two saves at once never overwrite each other: the second takes the number
after. S3's metadata is ASCII, so what ADK keeps with a version (its display
name, whether it's text, a referenced file's URI, your custom metadata) is
stored percent-encoded::

    artifacts = S3ArtifactService.from_url("s3://my-bucket/agents/", os.environ)
    runner = Runner(app=app, session_service=sessions, artifact_service=artifacts)

boto3 is synchronous, so each call runs in a thread.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterator, Mapping
from typing import Any
from urllib.parse import quote, unquote, urlparse

from google.adk.artifacts import artifact_util
from google.adk.artifacts.base_artifact_service import (
    ArtifactVersion,
    BaseArtifactService,
    ensure_part,
)
from google.adk.errors.input_validation_error import InputValidationError
from google.genai import types

__all__ = ["S3ArtifactService"]

log = logging.getLogger(__name__)

# What ADK keeps with a version, as S3 metadata (its keys are lower case).
DISPLAY_NAME = "adk-display-name"
IS_TEXT = "adk-is-text"
FILE_URI = "adk-file-uri"
FILE_MIME_TYPE = "adk-file-mime-type"
CUSTOM = "adk-custom-metadata"
# Saves racing for a version number before one gives up.
SAVE_ATTEMPTS = 10
# Seconds to reach S3, and to wait for each answer.
CONNECT_TIMEOUT = 5
READ_TIMEOUT = 60


def _encode(value: str) -> str:
    return quote(value, safe="")


def _decode(value: str | None) -> str | None:
    return unquote(value) if value is not None else None


def _version_of(key: str, prefix: str) -> int | None:
    """
    :return: The version a key holds of the artifact at ``prefix``; None when
        it's a different artifact's (a filename may hold ``/``, so ``a/``
        lists ``a/b/3`` too).
    """
    rest = key[len(prefix) :]
    if "/" in rest or not (rest.isascii() and rest.isdigit()):
        return None
    return int(rest)


class S3ArtifactService(BaseArtifactService):
    """
    Artifacts in an S3 bucket.

    :param bucket: The bucket.
    :param prefix: Where in it the artifacts go, e.g. ``agents/``; the whole bucket by default.
    :param client: A boto3 S3 client; one from the default credentials by default.
    :param create_bucket: Make the bucket on first use if it isn't there (a
        local S3 in development, which starts empty); otherwise it must exist.
    """

    def __init__(
        self,
        bucket: str,
        *,
        prefix: str = "",
        client: Any | None = None,
        create_bucket: bool = False,
    ) -> None:
        if client is None:
            import boto3

            client = boto3.client("s3")
        self.bucket = bucket
        self.prefix = f"{prefix.strip('/')}/" if prefix.strip("/") else ""
        self.client = client
        self._ready = not create_bucket

    @classmethod
    def from_url(
        cls,
        url: str,
        environment: Mapping[str, str] | None = None,
        *,
        create_bucket: bool = False,
    ) -> S3ArtifactService:
        """
        :param url: ``s3://bucket`` or ``s3://bucket/prefix``.
        :param environment: Where to find the AWS settings (``AWS_ACCESS_KEY_ID``,
            ``AWS_SECRET_ACCESS_KEY``, ``AWS_SESSION_TOKEN``, ``AWS_REGION``,
            ``AWS_PROFILE``, and ``AWS_ENDPOINT_URL_S3`` or ``AWS_ENDPOINT_URL``
            for S3 that isn't Amazon's); boto3's own lookup for what it doesn't have.
        :param create_bucket: Make the bucket on first use if it isn't there.
        :raises ValueError: It isn't an ``s3://`` URL.
        """
        import boto3
        from botocore.config import Config

        parsed = urlparse(url)
        if parsed.scheme != "s3" or not parsed.netloc:
            raise ValueError(f"Not an S3 URL (s3://bucket/prefix): {url}")
        env = environment or {}
        session = boto3.session.Session(
            aws_access_key_id=env.get("AWS_ACCESS_KEY_ID") or None,
            aws_secret_access_key=env.get("AWS_SECRET_ACCESS_KEY") or None,
            aws_session_token=env.get("AWS_SESSION_TOKEN") or None,
            region_name=env.get("AWS_REGION") or env.get("AWS_DEFAULT_REGION") or None,
            profile_name=env.get("AWS_PROFILE") or None,
        )
        endpoint = env.get("AWS_ENDPOINT_URL_S3") or env.get("AWS_ENDPOINT_URL") or None
        config = Config(
            connect_timeout=CONNECT_TIMEOUT,
            read_timeout=READ_TIMEOUT,
            retries={"mode": "standard"},
            # S3 that isn't Amazon's rarely has a host per bucket.
            s3={"addressing_style": "path"} if endpoint else None,
        )
        client = session.client("s3", endpoint_url=endpoint, config=config)
        return cls(parsed.netloc, prefix=parsed.path, client=client, create_bucket=create_bucket)

    def _ensure_bucket(self) -> None:
        """Makes the bucket, the first time, when asked to and it isn't there."""
        if self._ready:
            return
        from botocore.exceptions import ClientError

        try:
            self.client.head_bucket(Bucket=self.bucket)
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") not in (
                "404",
                "NoSuchBucket",
                "NotFound",
            ):
                raise
            try:
                self.client.create_bucket(Bucket=self.bucket)
                log.info("Made the artifacts bucket %s", self.bucket)
            except ClientError as made:
                code = made.response.get("Error", {}).get("Code")
                if code not in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
                    raise
        self._ready = True

    # --- Keys ---------------------------------------------------------------------

    def _artifact_prefix(
        self, app_name: str, user_id: str, filename: str, session_id: str | None
    ) -> str:
        """The key prefix of an artifact's versions, ending in ``/``."""
        artifact_util.validate_path_segment(app_name, "app_name")
        artifact_util.validate_path_segment(user_id, "user_id")
        if filename.startswith("user:"):
            return f"{self.prefix}{app_name}/{user_id}/user/{filename}/"
        if session_id is None:
            raise InputValidationError("Session ID must be provided for session-scoped artifacts.")
        artifact_util.validate_path_segment(session_id, "session_id")
        return f"{self.prefix}{app_name}/{user_id}/{session_id}/{filename}/"

    def _keys(self, prefix: str) -> Iterator[dict[str, Any]]:
        """Every object under a prefix, as list_objects_v2 describes them."""
        self._ensure_bucket()
        for page in self.client.get_paginator("list_objects_v2").paginate(
            Bucket=self.bucket, Prefix=prefix
        ):
            yield from page.get("Contents") or []

    def _versions(self, prefix: str) -> list[int]:
        found = (_version_of(item["Key"], prefix) for item in self._keys(prefix))
        return sorted(v for v in found if v is not None)

    def _head(self, key: str) -> dict[str, Any] | None:
        from botocore.exceptions import ClientError

        self._ensure_bucket()
        try:
            return dict(self.client.head_object(Bucket=self.bucket, Key=key))
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return None
            raise

    def _describe(self, key: str, version: int, head: Mapping[str, Any]) -> ArtifactVersion:
        metadata = head.get("Metadata") or {}
        custom = _decode(metadata.get(CUSTOM))
        return ArtifactVersion(
            version=version,
            canonical_uri=f"s3://{self.bucket}/{key}",
            create_time=head["LastModified"].timestamp(),
            mime_type=head.get("ContentType"),
            custom_metadata=json.loads(custom) if custom else {},
        )

    # --- Synchronous calls, each run in a thread ------------------------------------

    def _save(
        self,
        app_name: str,
        user_id: str,
        session_id: str | None,
        filename: str,
        artifact: types.Part | dict[str, Any],
        custom_metadata: dict[str, Any] | None,
    ) -> int:
        from botocore.exceptions import ClientError

        if not filename.startswith("user:"):
            if session_id is None:
                raise InputValidationError(
                    "Session ID must be provided for session-scoped artifacts."
                )
            artifact_util._validate_session_id_for_flat_storage(session_id)
        part = ensure_part(artifact)
        metadata: dict[str, str] = {}
        if custom_metadata:
            metadata[CUSTOM] = _encode(json.dumps(custom_metadata, default=str))
        body: bytes
        content_type: str | None
        if part.inline_data:
            if part.inline_data.data is None:
                raise InputValidationError("Artifact inline_data must contain data.")
            body, content_type = part.inline_data.data, part.inline_data.mime_type
            if part.inline_data.display_name:
                metadata[DISPLAY_NAME] = _encode(part.inline_data.display_name)
        elif part.text is not None:
            body, content_type = part.text.encode("utf-8"), "text/plain"
            metadata[IS_TEXT] = "true"
        elif part.file_data:
            uri = part.file_data.file_uri
            if not uri:
                raise InputValidationError("Artifact file_data must have a file_uri.")
            if artifact_util.is_artifact_ref(part):
                parsed = artifact_util.parse_artifact_uri(uri)
                if not parsed:
                    raise InputValidationError(f"Invalid artifact reference URI: {uri}")
                artifact_util.validate_artifact_reference_scope(
                    app_name=app_name, user_id=user_id, session_id=session_id, parsed_uri=parsed
                )
            # A reference: its URI is kept, nothing uploaded.
            metadata[FILE_URI] = _encode(uri)
            if part.file_data.mime_type:
                metadata[FILE_MIME_TYPE] = _encode(part.file_data.mime_type)
            body, content_type = b"", part.file_data.mime_type
        else:
            raise InputValidationError("Artifact must have either inline_data or text.")

        prefix = self._artifact_prefix(app_name, user_id, filename, session_id)
        versions = self._versions(prefix)
        version = versions[-1] + 1 if versions else 0
        extra = {"ContentType": content_type} if content_type else {}
        for attempt in range(SAVE_ATTEMPTS):
            try:
                self.client.put_object(
                    Bucket=self.bucket,
                    Key=f"{prefix}{version}",
                    Body=body,
                    Metadata=metadata,
                    # Only if no save took this number first.
                    IfNoneMatch="*",
                    **extra,
                )
                return version
            except ClientError as error:
                code = error.response.get("Error", {}).get("Code")
                if code not in ("PreconditionFailed", "ConditionalRequestConflict"):
                    raise
                if attempt == SAVE_ATTEMPTS - 1:
                    raise
                version += 1
        raise AssertionError("unreachable")

    def _load(
        self,
        app_name: str,
        user_id: str,
        session_id: str | None,
        filename: str,
        version: int | None,
        depth: int = artifact_util._MAX_ARTIFACT_REFERENCE_DEPTH,
    ) -> types.Part | None:
        from botocore.exceptions import ClientError

        prefix = self._artifact_prefix(app_name, user_id, filename, session_id)
        if version is None:
            versions = self._versions(prefix)
            if not versions:
                return None
            version = versions[-1]
        self._ensure_bucket()
        try:
            found = self.client.get_object(Bucket=self.bucket, Key=f"{prefix}{version}")
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return None
            raise
        metadata = found.get("Metadata") or {}
        content_type = found.get("ContentType")
        data = found["Body"].read()
        uri = _decode(metadata.get(FILE_URI))
        if uri:
            if uri.startswith("artifact://"):
                ref = artifact_util.resolve_artifact_reference(
                    file_uri=uri,
                    app_name=app_name,
                    user_id=user_id,
                    session_id=session_id,
                    remaining_depth=depth,
                )
                return self._load(
                    ref.app_name, ref.user_id, ref.session_id, ref.filename, ref.version, depth - 1
                )
            mime_type = _decode(metadata.get(FILE_MIME_TYPE)) or content_type
            return types.Part(file_data=types.FileData(file_uri=uri, mime_type=mime_type))
        if metadata.get(IS_TEXT) == "true":
            return types.Part(text=data.decode("utf-8"))
        display_name = _decode(metadata.get(DISPLAY_NAME))
        if display_name:
            return types.Part(
                inline_data=types.Blob(mime_type=content_type, data=data, display_name=display_name)
            )
        return types.Part.from_bytes(
            data=data, mime_type=content_type or "application/octet-stream"
        )

    def _list_keys(self, app_name: str, user_id: str, session_id: str | None) -> list[str]:
        artifact_util.validate_path_segment(app_name, "app_name")
        artifact_util.validate_path_segment(user_id, "user_id")
        prefixes = [f"{self.prefix}{app_name}/{user_id}/user/"]
        if session_id:
            artifact_util.validate_path_segment(session_id, "session_id")
            prefixes.append(f"{self.prefix}{app_name}/{user_id}/{session_id}/")
        names = set()
        for prefix in prefixes:
            for item in self._keys(prefix):
                # prefix + filename (which may hold /) + / + version
                filename, _, _ = item["Key"][len(prefix) :].rpartition("/")
                if filename:
                    names.add(filename)
        return sorted(names)

    def _delete(self, app_name: str, user_id: str, session_id: str | None, filename: str) -> None:
        prefix = self._artifact_prefix(app_name, user_id, filename, session_id)
        keys = [f"{prefix}{version}" for version in self._versions(prefix)]
        # delete_objects takes up to a thousand keys at a time.
        for start in range(0, len(keys), 1000):
            self.client.delete_objects(
                Bucket=self.bucket,
                Delete={"Objects": [{"Key": k} for k in keys[start : start + 1000]], "Quiet": True},
            )

    def _version(
        self,
        app_name: str,
        user_id: str,
        session_id: str | None,
        filename: str,
        version: int | None,
    ) -> ArtifactVersion | None:
        prefix = self._artifact_prefix(app_name, user_id, filename, session_id)
        if version is None:
            versions = self._versions(prefix)
            if not versions:
                return None
            version = versions[-1]
        key = f"{prefix}{version}"
        head = self._head(key)
        return self._describe(key, version, head) if head is not None else None

    def _all_versions(
        self, app_name: str, user_id: str, session_id: str | None, filename: str
    ) -> list[ArtifactVersion]:
        prefix = self._artifact_prefix(app_name, user_id, filename, session_id)
        out = []
        for version in self._versions(prefix):
            key = f"{prefix}{version}"
            head = self._head(key)
            if head is not None:
                out.append(self._describe(key, version, head))
        return out

    # --- BaseArtifactService ---------------------------------------------------------

    async def save_artifact(
        self,
        *,
        app_name: str,
        user_id: str,
        filename: str,
        artifact: types.Part | dict[str, Any],
        session_id: str | None = None,
        custom_metadata: dict[str, Any] | None = None,
    ) -> int:
        return await asyncio.to_thread(
            self._save, app_name, user_id, session_id, filename, artifact, custom_metadata
        )

    async def load_artifact(
        self,
        *,
        app_name: str,
        user_id: str,
        filename: str,
        session_id: str | None = None,
        version: int | None = None,
    ) -> types.Part | None:
        return await asyncio.to_thread(self._load, app_name, user_id, session_id, filename, version)

    async def list_artifact_keys(
        self, *, app_name: str, user_id: str, session_id: str | None = None
    ) -> list[str]:
        return await asyncio.to_thread(self._list_keys, app_name, user_id, session_id)

    async def delete_artifact(
        self, *, app_name: str, user_id: str, filename: str, session_id: str | None = None
    ) -> None:
        await asyncio.to_thread(self._delete, app_name, user_id, session_id, filename)

    async def list_versions(
        self, *, app_name: str, user_id: str, filename: str, session_id: str | None = None
    ) -> list[int]:
        prefix = self._artifact_prefix(app_name, user_id, filename, session_id)
        return await asyncio.to_thread(self._versions, prefix)

    async def list_artifact_versions(
        self, *, app_name: str, user_id: str, filename: str, session_id: str | None = None
    ) -> list[ArtifactVersion]:
        return await asyncio.to_thread(self._all_versions, app_name, user_id, session_id, filename)

    async def get_artifact_version(
        self,
        *,
        app_name: str,
        user_id: str,
        filename: str,
        session_id: str | None = None,
        version: int | None = None,
    ) -> ArtifactVersion | None:
        return await asyncio.to_thread(
            self._version, app_name, user_id, session_id, filename, version
        )
