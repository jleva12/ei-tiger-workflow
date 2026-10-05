"""Where knowledge base documents are stored: an S3 bucket the async
worker's documents task reads them from. boto3 is synchronous, so its calls
run in a thread.

Every file's key follows the hierarchy by ID, so each level is a prefix:
``forge/org-<id>/kb-<id>/documents/<document>/<name>``. Listing
``forge/org-<id>/`` finds all of an organization's files, adding
``kb-<id>/`` a knowledge base's. IDs never change and knowledge bases never
move to another organization, so a key stays true for as long as its file
exists.
"""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import IO, Any, Self

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from forge_admin.config import Settings

# Seconds to reach S3, and to wait for each answer.
CONNECT_TIMEOUT = 5
READ_TIMEOUT = 60
# The first part of every key, leaving the rest of the bucket to others.
ROOT = "forge"
# Bytes read from S3 at a time while a stored file is sent on.
READ_CHUNK = 256 * 1024


def scope_prefix(organization_id: str, knowledge_base_id: str | None = None) -> str:
    """
    The key prefix of everything stored at one level of the hierarchy.

    :param organization_id: The organization.
    :param knowledge_base_id: One of its knowledge bases, to narrow to it.
    :return: E.g. ``forge/org-<id>/kb-<id>/`` for a knowledge base.
    """
    parts = [ROOT, f"org-{organization_id}"]
    if knowledge_base_id is not None:
        parts.append(f"kb-{knowledge_base_id}")
    return "/".join(parts) + "/"


def document_key(
    *,
    organization_id: str,
    knowledge_base_id: str,
    document_id: str,
    name: str,
) -> str:
    """
    :param name: The file's name as it may appear in a key.
    :return: Where a knowledge base's document is stored, under its prefix.
    """
    prefix = scope_prefix(organization_id, knowledge_base_id)
    return f"{prefix}documents/{document_id}/{name}"


class StorageError(Exception):
    """
    S3 refused a call, or could not be reached.

    :ivar code: S3's error code, e.g. ``AccessDenied``; ``unreachable`` when
        it could not be reached.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass
class StoredFile:
    """
    A stored file being read: its size, and its bytes a chunk at a time.
    Read ``chunks()`` to the end, or ``close()`` it, to let the connection go.

    :ivar size: Its length in bytes.
    """

    size: int
    _body: Any

    async def chunks(self) -> AsyncIterator[bytes]:
        """:return: The file's bytes in order, read in a thread."""
        try:
            while chunk := await asyncio.to_thread(self._body.read, READ_CHUNK):
                yield chunk
        finally:
            self.close()

    def close(self) -> None:
        self._body.close()


class DocumentStore:
    """
    Stores knowledge base documents' files in the documents bucket.

    :param client: A boto3 S3 client.
    :param bucket: The documents bucket.
    :param create_bucket: Create the bucket when it's missing.
    """

    def __init__(self, client: Any, bucket: str, *, create_bucket: bool) -> None:
        self._client = client
        self.bucket = bucket
        self._create_bucket = create_bucket

    @classmethod
    def from_settings(cls, settings: Settings) -> Self | None:
        """
        :param settings: Settings with ``documents_bucket`` and the S3 service.
        :return: A store, or None when uploads aren't set up.
        """
        if settings.documents_bucket is None:
            return None
        options: dict[str, Any] = {
            "region_name": settings.s3_region,
            "config": Config(
                connect_timeout=CONNECT_TIMEOUT,
                read_timeout=READ_TIMEOUT,
                retries={"max_attempts": 3, "mode": "standard"},
                # S3-compatible servers are addressed by path, not subdomain.
                s3={"addressing_style": "path"} if settings.s3_endpoint_url else {},
            ),
        }
        if settings.s3_endpoint_url:
            options["endpoint_url"] = settings.s3_endpoint_url
        if settings.s3_access_key_id and settings.s3_secret_access_key:
            options["aws_access_key_id"] = settings.s3_access_key_id
            options["aws_secret_access_key"] = (
                settings.s3_secret_access_key.get_secret_value()
            )
        return cls(
            boto3.client("s3", **options),
            settings.documents_bucket,
            create_bucket=settings.s3_create_bucket,
        )

    def uri(self, key: str) -> str:
        """:return: Where the worker reads the object, ``s3://<bucket>/<key>``."""
        return f"s3://{self.bucket}/{key}"

    async def put(self, key: str, body: IO[bytes], *, media_type: str) -> None:
        """
        Store a file, creating the bucket first when it's missing and that's
        allowed.

        :param key: The object's key.
        :param body: The file, read from the start.
        :param media_type: Its media type.
        :raises StorageError: S3 refused or could not be reached.
        """
        try:
            await asyncio.to_thread(self._put, key, body, media_type)
        except ClientError as error:
            if _code(error) != "NoSuchBucket" or not self._create_bucket:
                raise _refused(error) from None
            await self._make_bucket()
            try:
                await asyncio.to_thread(self._put, key, body, media_type)
            except ClientError as again:
                raise _refused(again) from None
        except BotoCoreError as error:
            raise StorageError("unreachable", str(error)) from None

    async def open(self, key: str) -> StoredFile:
        """
        Start reading a stored file.

        :param key: The object's key.
        :return: The file, to read and then close.
        :raises StorageError: S3 refused (``NoSuchKey`` for a missing file) or
            could not be reached.
        """
        try:
            found = await asyncio.to_thread(
                self._client.get_object, Bucket=self.bucket, Key=key
            )
        except ClientError as error:
            raise _refused(error) from None
        except BotoCoreError as error:
            raise StorageError("unreachable", str(error)) from None
        return StoredFile(size=int(found["ContentLength"]), _body=found["Body"])

    def key_of(self, uri: str) -> str | None:
        """:return: The key of an ``s3://`` URI in this bucket; None for another."""
        prefix = f"s3://{self.bucket}/"
        return uri.removeprefix(prefix) if uri.startswith(prefix) else None

    async def delete(self, key: str) -> None:
        """
        :param key: The object to remove; a missing one is fine.
        :raises StorageError: S3 refused or could not be reached.
        """
        try:
            await asyncio.to_thread(
                self._client.delete_object, Bucket=self.bucket, Key=key
            )
        except ClientError as error:
            raise _refused(error) from None
        except BotoCoreError as error:
            raise StorageError("unreachable", str(error)) from None

    def close(self) -> None:
        self._client.close()

    def _put(self, key: str, body: IO[bytes], media_type: str) -> None:
        body.seek(0)
        self._client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=body,
            ContentType=media_type or "application/octet-stream",
        )

    async def _make_bucket(self) -> None:
        options: dict[str, Any] = {"Bucket": self.bucket}
        region = self._client.meta.region_name
        if region and region != "us-east-1":
            options["CreateBucketConfiguration"] = {"LocationConstraint": region}
        try:
            await asyncio.to_thread(self._client.create_bucket, **options)
        except ClientError as error:
            # Another request created it first.
            if _code(error) not in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
                raise _refused(error) from None


def _code(error: ClientError) -> str:
    return str(error.response.get("Error", {}).get("Code", ""))


def _refused(error: ClientError) -> StorageError:
    return StorageError(
        _code(error) or "error",
        str(error.response.get("Error", {}).get("Message", "")),
    )
